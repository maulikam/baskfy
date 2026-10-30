//! The backfills against a throwaway clone of the bed: `test_bhavcopy_backfill.py`,
//! `test_pipeline_acceptance.py`'s resume criterion, `test_deep_backfill.py`, `test_index_repair.py`,
//! `test_eq_bars_worker.py`, plus breadth and index-level runs — resume and re-run identity included.
//!
//! Run: `BASKFY_RS_BACKFILLS_DB_URL=postgres://…/rst_… cargo test -p baskfy-worker --test
//! backfills_db -- --ignored`. Providers are stubs; nothing leaves the box.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::too_many_lines
)]

mod backfills_support;

use std::collections::BTreeMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use backfills_support::{
    BhavRow, LOCK, Stub, bars_of, bhavcopy, d, daily_frame, db, dec, exec, make_instrument, tag,
    weekdays,
};
use baskfy_db::WriteDb;
use baskfy_providers::ProviderError;
use baskfy_providers::kite::MinuteBound;
use baskfy_providers::records::{
    IndexSnapshot, IndexSnapshotFields, MinuteBarFields, MinuteBarRecord, PyDateTime,
};
use baskfy_providers::tokens::ist;
use baskfy_worker::tasks::backfills::eq_bars::{self, EqName, MinuteReader};
use baskfy_worker::tasks::backfills::index_repair::{self, SnapshotSource};
use baskfy_worker::tasks::backfills::{bhavcopy as bhav, breadth, cursor, deep, index_backfill};
use baskfy_worker::window::DateWindow;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

async fn scalar_i64(db: &WriteDb, sql: &str) -> i64 {
    let mut tx = db.begin().await.unwrap();
    let v: i64 = sqlx::query_scalar(sql).fetch_one(&mut *tx).await.unwrap();
    tx.rollback().await.unwrap();
    v
}

// ---------------------------------------------------------------------------------------------
// bhavcopy_backfill
// ---------------------------------------------------------------------------------------------

type FullBar = (
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<i64>,
    Option<i64>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<String>,
    Option<Decimal>,
);

async fn full_bar(db: &WriteDb, id: i64, on: NaiveDate) -> Option<FullBar> {
    let mut tx = db.begin().await.unwrap();
    let row = sqlx::query_as(
        "SELECT open, close, close_raw, open_raw, volume, volume_raw, turnover, upper_circuit, \
         lower_circuit, adj_factor, source, high_raw FROM ohlcv_daily \
         WHERE instrument_id = $1 AND date = $2",
    )
    .bind(id)
    .bind(on)
    .fetch_optional(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    row
}

fn row<'a>(
    symbol: &'a str,
    series: &'a str,
    turnover: Option<&'a str>,
    bands: [Option<&'a str>; 2],
) -> BhavRow<'a> {
    BhavRow {
        symbol,
        series: Some(series),
        ohlc: ["273.0000", "280.5000", "271.1000", "278.4500"],
        volume: 1_234_567,
        turnover,
        bands,
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn bhavcopy_backfill_maps_joins_filters_and_reruns_identically() {
    let _g = LOCK.lock().await;
    let db = db();
    let t = tag();
    let (a, b, c) = (format!("{t}A"), format!("{t}B"), format!("{t}C"));
    let (govt, nosuch) = (format!("{t}G"), format!("{t}X"));
    let ia = make_instrument(&db, &a, "EQ", None).await;
    let ib = make_instrument(&db, &b, "BE", None).await;
    let ic = make_instrument(&db, &c, "SM", None).await;
    let day = d(2026, 8, 18);
    let band = [Some("299.2000"), Some("244.8000")];
    let frame = bhavcopy(
        day,
        &[
            row(&a, "EQ", Some("18768602294.72"), band),
            row(&a, "BE", None, band),
            row(&b, "BE", None, band),
            row(&c, "SM", None, [None, None]),
            row(&govt, "GS", None, band),
            row(&nosuch, "EQ", None, band),
        ],
    );
    let mut stub = Stub::default();
    stub.bhavcopy
        .insert(d(2026, 8, 14), Err("503 upstream wobble".to_owned()));
    stub.bhavcopy.insert(day, Ok(frame));
    let stub = Arc::new(stub);
    let window = DateWindow::new(d(2026, 8, 14), day).unwrap();

    let report =
        bhav::backfill_bars_from_bhavcopy(&db, &stub.composite(), window, &bhav::EQUITY_SERIES, 0)
            .await
            .unwrap();
    assert_eq!(report.trading_days, 3);
    assert_eq!(report.missing_days, vec![d(2026, 8, 14), d(2026, 8, 17)]);
    let failed: Vec<&str> = report.failures.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(failed, vec!["2026-08-14"], "a 404 is missing, not failed");
    assert!(!report.succeeded());
    assert_eq!((report.days_written, report.bars_written), (1, 3));
    assert!(report.unmatched_symbols.contains(&format!("{a}:BE")));
    assert!(report.unmatched_symbols.contains(&format!("{nosuch}:EQ")));
    assert!(
        !report
            .unmatched_symbols
            .iter()
            .any(|u| u.starts_with(&govt))
    );

    let stored = full_bar(&db, ia, day).await.unwrap();
    assert_eq!(stored.1, Some(dec("278.4500")));
    assert_eq!(stored.1, stored.2, "close starts equal to close_raw");
    assert_eq!(stored.0, stored.3);
    assert_eq!((stored.4, stored.5), (Some(1_234_567), Some(1_234_567)));
    assert_eq!(stored.6, Some(dec("18768602294.72")));
    assert_eq!(
        (stored.7, stored.8),
        (Some(dec("299.2")), Some(dec("244.8")))
    );
    assert_eq!(stored.9, Some(Decimal::ONE));
    assert_eq!(stored.10.as_deref(), Some("nse"));
    assert_eq!(stored.11, Some(dec("280.5")));
    assert!(
        full_bar(&db, ib, day).await.is_some(),
        "BE joins on symbol and series"
    );
    let sme = full_bar(&db, ic, day).await.unwrap();
    assert_eq!(
        (sme.7, sme.8),
        (None, None),
        "an unpublished band stays NULL"
    );

    let before = (
        bars_of(&db, ia).await,
        bars_of(&db, ib).await,
        bars_of(&db, ic).await,
    );
    let again =
        bhav::backfill_bars_from_bhavcopy(&db, &stub.composite(), window, &bhav::EQUITY_SERIES, 0)
            .await
            .unwrap();
    assert_eq!(again.bars_written, 3);
    let after = (
        bars_of(&db, ia).await,
        bars_of(&db, ib).await,
        bars_of(&db, ic).await,
    );
    assert_eq!(
        before, after,
        "house rule 7: a re-run writes identical rows"
    );
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn bhavcopy_series_narrow_what_is_written_and_reported() {
    let _g = LOCK.lock().await;
    let db = db();
    let t = tag();
    let (a, b) = (format!("{t}A"), format!("{t}B"));
    let ia = make_instrument(&db, &a, "EQ", None).await;
    let ib = make_instrument(&db, &b, "BE", None).await;
    let day = d(2026, 8, 18);
    let band = [None, None];
    let mut stub = Stub::default();
    stub.bhavcopy.insert(
        day,
        Ok(bhavcopy(
            day,
            &[row(&a, "EQ", None, band), row(&b, "BE", None, band)],
        )),
    );
    let stub = Arc::new(stub);
    let report = bhav::backfill_bars_from_bhavcopy(
        &db,
        &stub.composite(),
        DateWindow::single(day),
        &["EQ"],
        0,
    )
    .await
    .unwrap();
    assert_eq!(report.bars_written, 1);
    assert!(report.unmatched_symbols.is_empty());
    assert!(full_bar(&db, ia, day).await.is_some());
    assert!(full_bar(&db, ib, day).await.is_none());
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// backfill (the cursor table)
// ---------------------------------------------------------------------------------------------

fn options(symbols: &[String], resume: bool, stop_after: Option<usize>) -> cursor::Options<'_> {
    cursor::Options {
        symbols: Some(symbols),
        concurrency: 1,
        resume,
        chunk_days: 3,
        stop_after,
    }
}

async fn cursor_counts(db: &WriteDb) -> (i64, i64) {
    let done = scalar_i64(
        db,
        "SELECT count(*) FROM ingest_cursor WHERE kind = 'bars' AND status = 'done'",
    )
    .await;
    let other = scalar_i64(
        db,
        "SELECT count(*) FROM ingest_cursor WHERE kind = 'bars' AND status <> 'done'",
    )
    .await;
    (done, other)
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn a_killed_backfill_resumes_to_the_state_of_an_uninterrupted_one() {
    let _g = LOCK.lock().await;
    let db = db();
    exec(&db, "DELETE FROM ingest_cursor").await;
    let t = tag();
    let (start, end) = (d(2026, 4, 6), d(2026, 4, 17));
    let mut stub = Stub::default();
    let mut ids = Vec::new();
    let mut symbols = Vec::new();
    for k in 0..3_i64 {
        let symbol = format!("{t}{k}");
        let token = 7_100_000 + k;
        ids.push(make_instrument(&db, &symbol, "EQ", Some(token)).await);
        let candles: Vec<_> = weekdays(start, end)
            .into_iter()
            .map(|day| (day, "100.5", "101", "99", "100.25", 10 + k))
            .collect();
        stub.bars.insert(token, daily_frame(&symbol, &candles));
        symbols.push(symbol);
    }
    let stub = Arc::new(stub);
    let window = DateWindow::new(start, end).unwrap();

    let killed = cursor::run_backfill(
        &db,
        Arc::new(stub.composite()),
        window,
        options(&symbols, false, Some(2)),
    )
    .await
    .unwrap();
    assert_eq!(killed.planned, 12);
    assert_eq!(killed.completed, 2);
    assert_eq!(stub.fetches(), 2);
    let (done, outstanding) = cursor_counts(&db).await;
    assert_eq!(done, 2);
    assert!(outstanding > 0, "an interrupted run leaves work pending");

    let resumed = cursor::run_backfill(
        &db,
        Arc::new(stub.composite()),
        window,
        options(&symbols, true, None),
    )
    .await
    .unwrap();
    assert_eq!(resumed.already_done, 2);
    assert_eq!(resumed.completed, 10);
    assert_eq!(
        stub.fetches(),
        12,
        "resuming never re-fetches a finished unit"
    );
    assert_eq!(cursor_counts(&db).await, (12, 0));
    let mut resumed_rows = Vec::new();
    for id in &ids {
        let rows = bars_of(&db, *id).await;
        assert_eq!(rows.len(), 10);
        assert!(rows.iter().all(|r| r.4 == Some(Decimal::ONE) && r.2 == r.3));
        resumed_rows.push(rows);
    }

    let full = cursor::run_backfill(
        &db,
        Arc::new(stub.composite()),
        window,
        options(&symbols, false, None),
    )
    .await
    .unwrap();
    assert_eq!(full.completed, 12);
    for (id, rows) in ids.iter().zip(&resumed_rows) {
        assert_eq!(
            &bars_of(&db, *id).await,
            rows,
            "same final state as one pass"
        );
    }
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn a_unit_without_a_token_is_done_and_a_failing_one_is_retried_on_resume() {
    let _g = LOCK.lock().await;
    let db = db();
    exec(&db, "DELETE FROM ingest_cursor").await;
    let t = tag();
    let (none_sym, bad_sym) = (format!("{t}N"), format!("{t}F"));
    make_instrument(&db, &none_sym, "EQ", None).await;
    make_instrument(&db, &bad_sym, "EQ", Some(7_200_001)).await;
    let stub = Arc::new(Stub {
        failing_tokens: vec![7_200_001],
        ..Stub::default()
    });
    let symbols = vec![none_sym.clone(), bad_sym.clone()];
    let window = DateWindow::new(d(2026, 4, 6), d(2026, 4, 8)).unwrap();
    let report = cursor::run_backfill(
        &db,
        Arc::new(stub.composite()),
        window,
        options(&symbols, true, None),
    )
    .await
    .unwrap();
    assert_eq!((report.completed, report.failed), (1, 1));
    assert!(report.errors[0].starts_with(&format!("{bad_sym} 2026-04-06..2026-04-08: ")));
    let mut tx = db.begin().await.unwrap();
    let rows: Vec<(String, String, i64, Option<serde_json::Value>)> = sqlx::query_as(
        "SELECT i.symbol, c.status, c.attempts, c.last_error FROM ingest_cursor c \
         JOIN instrument i ON i.id = c.instrument_id ORDER BY i.symbol",
    )
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    let by: BTreeMap<_, _> = rows.into_iter().map(|r| (r.0.clone(), r)).collect();
    assert_eq!(by[&none_sym].1, "done");
    assert_eq!(
        by[&none_sym].3.as_ref().unwrap()["message"],
        "no provider token"
    );
    assert_eq!((by[&bad_sym].1.as_str(), by[&bad_sym].2), ("failed", 1));
    let again = cursor::run_backfill(
        &db,
        Arc::new(stub.composite()),
        window,
        options(&symbols, true, None),
    )
    .await
    .unwrap();
    assert_eq!((again.already_done, again.failed), (1, 1));
    assert_eq!(
        scalar_i64(&db, "SELECT max(attempts) FROM ingest_cursor").await,
        2
    );
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// deep_backfill
// ---------------------------------------------------------------------------------------------

async fn seed_bar(db: &WriteDb, id: i64, on: NaiveDate, close: &str) {
    let mut tx = db.begin().await.unwrap();
    sqlx::query(
        "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, close_raw, \
         volume_raw, adj_factor, source) VALUES ($1, $2, $3, $3, $3, $3, 1, $3, 1, 1, 'nse')",
    )
    .bind(id)
    .bind(on)
    .bind(dec(close))
    .execute(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn deep_history_is_spliced_marked_fill_only_and_idempotent() {
    let _g = LOCK.lock().await;
    let db = db();
    let t = tag();
    let symbol = format!("{t}D");
    let token = 7_300_001;
    let id = make_instrument(&db, &symbol, "EQ", Some(token)).await;
    seed_bar(&db, id, d(2026, 4, 1), "105").await;
    seed_bar(&db, id, d(2026, 4, 2), "106").await;
    let mut stub = Stub::default();
    stub.bars.insert(
        token,
        daily_frame(
            &symbol,
            &[
                (d(2013, 1, 1), "10", "11", "9", "10", 100),
                (d(2013, 1, 2), "0", "0", "0", "0", 0),
                (d(2013, 1, 3), "11", "12", "10", "11", 200),
                (d(2026, 4, 1), "100", "100", "100", "100", 5),
            ],
        ),
    );
    let stub = Arc::new(stub);
    let provider = stub.composite();
    let symbols = vec![symbol.clone()];
    let (start, end) = (d(2013, 1, 1), d(2026, 4, 2));

    let dry = deep::run(&db, &provider, false, start, end, Some(&symbols), None)
        .await
        .unwrap();
    assert_eq!(
        (dry.instruments, dry.fetched, dry.written, dry.spliced),
        (1, 4, 2, 1)
    );
    assert_eq!(bars_of(&db, id).await.len(), 2, "a dry run writes nothing");

    let wrote = deep::run(&db, &provider, true, start, end, Some(&symbols), None)
        .await
        .unwrap();
    assert_eq!(wrote.written, 2);
    assert!(wrote.errors.is_empty(), "{:?}", wrote.errors);
    let rows = bars_of(&db, id).await;
    let dates: Vec<NaiveDate> = rows.iter().map(|r| r.0).collect();
    assert_eq!(
        dates,
        vec![d(2013, 1, 1), d(2013, 1, 3), d(2026, 4, 1), d(2026, 4, 2)],
        "the zero placeholder never lands"
    );
    assert_eq!(
        rows[0].2,
        Some(dec("10.5")),
        "anchored to our level at the seam"
    );
    assert_eq!(rows[1].2, Some(dec("11.55")));
    for r in &rows[..2] {
        assert_eq!(r.6.as_deref(), Some("kite_adjusted"));
        assert_eq!(r.4, Some(Decimal::ONE));
        assert_eq!(r.2, r.3);
    }
    assert_eq!(rows[2].2, Some(dec("105")));
    assert_eq!(
        rows[2].6.as_deref(),
        Some("nse"),
        "a bhavcopy bar is never overwritten"
    );

    let again = deep::run(&db, &provider, true, start, end, Some(&symbols), None)
        .await
        .unwrap();
    assert_eq!(again.skipped_no_deep_history, 1);
    assert!(again.errors.is_empty());
    assert_eq!(bars_of(&db, id).await, rows, "house rule 7");
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn deep_history_refuses_a_compressed_chunk_and_writes_only_where_absent() {
    let _g = LOCK.lock().await;
    let db = db();
    let t = tag();
    let symbol = format!("{t}Z");
    let token = 7_300_002;
    let id = make_instrument(&db, &symbol, "EQ", Some(token)).await;
    let mut stub = Stub::default();
    stub.bars.insert(
        token,
        daily_frame(&symbol, &[(d(2018, 6, 15), "10", "10", "10", "10", 1)]),
    );
    let stub = Arc::new(stub);
    let symbols = vec![symbol.clone()];
    let report = deep::run(
        &db,
        &stub.composite(),
        true,
        d(2018, 6, 1),
        d(2018, 6, 30),
        Some(&symbols),
        None,
    )
    .await
    .unwrap();
    assert_eq!((report.skipped_compressed, report.written), (1, 0));
    assert!(report.compressed_ranges.contains("2018-04-19..2019-04-14"));
    assert!(bars_of(&db, id).await.is_empty());

    let mut tx = db.begin().await.unwrap();
    assert!(
        deep::compressed_ranges_covering(&mut tx, &[])
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        deep::compressed_ranges_covering(&mut tx, &[d(2026, 4, 6), d(2026, 6, 3)])
            .await
            .unwrap()
            .is_empty()
    );
    tx.rollback().await.unwrap();

    seed_bar(&db, id, d(2026, 4, 6), "500").await;
    let mut tx = db.begin().await.unwrap();
    let offered = deep::write(&mut tx, id, &[(d(2026, 4, 6), [1.0, 1.0, 1.0, 1.0, 1.0])])
        .await
        .unwrap();
    tx.commit().await.unwrap();
    assert_eq!(offered, 1);
    let rows = bars_of(&db, id).await;
    assert_eq!(rows[0].3, Some(dec("500")));
    assert_eq!(rows[0].6.as_deref(), Some("nse"));
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// breadth_backfill
// ---------------------------------------------------------------------------------------------

type Health = Vec<(i16, Option<Decimal>, Option<Decimal>, Option<i32>)>;

async fn health_on(db: &WriteDb, on: NaiveDate) -> Health {
    let mut tx = db.begin().await.unwrap();
    let rows = sqlx::query_as(
        "SELECT index_id, pct_above_200dma, pct_above_50dma, constituent_count \
         FROM market_health_daily WHERE date = $1 ORDER BY index_id",
    )
    .bind(on)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    rows
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn breadth_carries_membership_as_derived_and_reruns_identically() {
    let _g = LOCK.lock().await;
    let db = db();
    let on = d(2021, 6, 30);
    let first = breadth::run(&db, true, on, on, 1).await.unwrap();
    assert!(first.errors.is_empty(), "{:?}", first.errors);
    assert_eq!((first.dates, first.skipped_have_factors), (1, 1));
    assert_eq!(
        first.factors_computed, 0,
        "a date that has factors is not recomputed"
    );
    assert!(first.membership_rows > 0);
    assert!(first.breadth_rows > 0);
    let carried = scalar_i64(
        &db,
        "SELECT count(*) FROM index_member_daily WHERE date = '2021-06-30' AND source = 'derived'",
    )
    .await;
    let all = scalar_i64(
        &db,
        "SELECT count(*) FROM index_member_daily WHERE date = '2021-06-30'",
    )
    .await;
    assert_eq!(
        (carried, all),
        (first.membership_rows, first.membership_rows)
    );
    let health = health_on(&db, on).await;
    assert!(!health.is_empty());

    let second = breadth::run(&db, true, on, on, 1).await.unwrap();
    assert_eq!(
        (second.membership_rows, second.breadth_rows),
        (first.membership_rows, first.breadth_rows)
    );
    assert_eq!(
        scalar_i64(
            &db,
            "SELECT count(*) FROM index_member_daily WHERE date = '2021-06-30'",
        )
        .await,
        all
    );
    assert_eq!(health_on(&db, on).await, health, "house rule 7");

    let sampled = breadth::run(&db, false, d(2021, 6, 1), d(2021, 6, 30), 5)
        .await
        .unwrap();
    let sessions = scalar_i64(
        &db,
        "SELECT count(*) FROM trading_day WHERE exchange_id = 1 AND is_trading_day \
         AND date BETWEEN '2021-06-01' AND '2021-06-30'",
    )
    .await;
    assert_eq!(
        i64::try_from(sampled.dates).unwrap(),
        (sessions + 4) / 5,
        "one trading day in five"
    );
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// index_backfill
// ---------------------------------------------------------------------------------------------

async fn make_index(db: &WriteDb, slug: &str, name: &str) -> i16 {
    let mut tx = db.begin().await.unwrap();
    let id: i16 = sqlx::query_scalar(
        "INSERT INTO index_def (id, slug, name, is_universe, sort_order) \
         SELECT max(id) + 1, $1, $2, false, 0 FROM index_def RETURNING id",
    )
    .bind(slug)
    .bind(name)
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    tx.commit().await.unwrap();
    id
}

type Level = (
    NaiveDate,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
    Option<Decimal>,
);

async fn levels_of(db: &WriteDb, index_id: i16) -> Vec<Level> {
    let mut tx = db.begin().await.unwrap();
    let rows = sqlx::query_as(
        "SELECT date, level, change_abs, change_pct, pe FROM index_snapshot_daily \
         WHERE index_id = $1 ORDER BY date",
    )
    .bind(index_id)
    .fetch_all(&mut *tx)
    .await
    .unwrap();
    tx.rollback().await.unwrap();
    rows
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn index_levels_match_by_name_overwrite_level_and_keep_fundamentals() {
    let _g = LOCK.lock().await;
    let db = db();
    let t = tag();
    let id = make_index(
        &db,
        &format!("{}-smallcap-250", t.to_lowercase()),
        &format!("{t} SMALLCAP 250"),
    )
    .await;
    exec(
        &db,
        &format!(
            "INSERT INTO index_snapshot_daily (index_id, date, level, pe) \
             VALUES ({id}, '2005-01-05', 1, 20.37)"
        ),
    )
    .await;
    let token = 7_400_001;
    let mut stub = Stub::default();
    stub.bars.insert(
        token,
        daily_frame(
            "",
            &[
                (d(2005, 1, 3), "100", "100", "100", "100", 0),
                (d(2005, 1, 4), "0", "0", "0", "0", 0),
                (d(2005, 1, 5), "102", "102", "102", "102", 0),
                (d(2005, 1, 6), "103", "103", "103", "103", 0),
            ],
        ),
    );
    let stub = Arc::new(stub);
    let kite = vec![
        (format!("{t} SMLCAP 250"), token),
        (format!("{t} NOTHING"), 7_400_002),
    ];
    let (start, end) = (d(2005, 1, 1), d(2005, 1, 10));

    let dry = index_backfill::run(&db, &stub.composite(), &kite, false, start, end)
        .await
        .unwrap();
    assert_eq!((dry.matched, dry.written), (1, 3));
    assert_eq!(levels_of(&db, id).await.len(), 1);

    let report = index_backfill::run(&db, &stub.composite(), &kite, true, start, end)
        .await
        .unwrap();
    assert_eq!(
        (report.kite_indices, report.matched, report.written),
        (2, 1, 3)
    );
    assert_eq!(report.unmatched_kite, vec![format!("{t} NOTHING")]);
    assert!(report.errors.is_empty());
    let rows = levels_of(&db, id).await;
    assert_eq!(
        rows,
        vec![
            (d(2005, 1, 3), Some(dec("100")), None, None, None),
            (
                d(2005, 1, 5),
                Some(dec("102")),
                None,
                None,
                Some(dec("20.37"))
            ),
            (
                d(2005, 1, 6),
                Some(dec("103")),
                Some(dec("1")),
                Some(dec("0.9804")),
                None
            ),
        ],
        "a zero close resets the change chain; pe is NSE's and survives"
    );
    index_backfill::run(&db, &stub.composite(), &kite, true, start, end)
        .await
        .unwrap();
    assert_eq!(levels_of(&db, id).await, rows, "house rule 7");
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// index_repair
// ---------------------------------------------------------------------------------------------

struct Nse {
    levels: BTreeMap<NaiveDate, Vec<(String, &'static str)>>,
    failing: Vec<NaiveDate>,
    asked: Mutex<Vec<NaiveDate>>,
}

impl SnapshotSource for Nse {
    fn index_snapshots(
        &self,
        on: NaiveDate,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<IndexSnapshot>, ProviderError>> + Send + '_>> {
        Box::pin(async move {
            self.asked.lock().unwrap().push(on);
            if self.failing.contains(&on) {
                return Err(ProviderError::unexpected_payload(format!(
                    "GET ind_close_all_{}.csv returned 404",
                    on.format("%d%m%Y")
                )));
            }
            Ok(self
                .levels
                .get(&on)
                .map(|rows| {
                    rows.iter()
                        .map(|(name, level)| {
                            let mut f = IndexSnapshotFields::new(name, on);
                            f.level = Some(dec(level));
                            f.pe = Some(dec("20.37"));
                            IndexSnapshot::new(f).unwrap()
                        })
                        .collect()
                })
                .unwrap_or_default())
        })
    }
}

const ANCHOR: (i32, u32, u32) = (2026, 7, 7);
const REAL: [[&str; 2]; 4] = [
    ["24398.70", "23368.85"],
    ["24476.10", "23440.25"],
    ["24512.40", "23490.90"],
    ["24388.15", "23401.05"],
];
const FIXTURE: [[&str; 2]; 3] = [
    ["1004.6026", "1576.9012"],
    ["1022.5254", "1588.8075"],
    ["1014.1944", "1606.5549"],
];

struct Repair {
    slugs: [String; 2],
    names: [String; 2],
    ids: [i16; 2],
}

fn day_n(n: usize) -> NaiveDate {
    d(ANCHOR.0, ANCHOR.1, ANCHOR.2) + Duration::days(i64::try_from(n).unwrap())
}

async fn repair_bed(db: &WriteDb, corrupt: bool) -> Repair {
    let t = tag();
    let names = [format!("{t} Nifty 50"), format!("{t} Nifty 500")];
    let slugs = [
        format!("{}-nifty-50", t.to_lowercase()),
        format!("{}-nifty-500", t.to_lowercase()),
    ];
    let ids = [
        make_index(db, &slugs[0], &names[0]).await,
        make_index(db, &slugs[1], &names[1]).await,
    ];
    for k in 0..2 {
        let mut sql = format!(
            "INSERT INTO index_snapshot_daily (index_id, date, level) VALUES ({}, '{}', {})",
            ids[k],
            day_n(0),
            REAL[0][k]
        );
        if corrupt {
            for (n, row) in FIXTURE.iter().enumerate() {
                sql.push_str(&format!(", ({}, '{}', {})", ids[k], day_n(n + 1), row[k]));
            }
        }
        exec(db, &sql).await;
    }
    Repair { slugs, names, ids }
}

fn nse(r: &Repair, table: &[[&'static str; 2]], first_day: usize, failing: &[NaiveDate]) -> Nse {
    let mut levels = BTreeMap::new();
    for (n, row) in table.iter().enumerate() {
        levels.insert(
            day_n(first_day + n),
            vec![(r.names[0].clone(), row[0]), (r.names[1].clone(), row[1])],
        );
    }
    Nse {
        levels,
        failing: failing.to_vec(),
        asked: Mutex::new(Vec::new()),
    }
}

async fn index_levels(db: &WriteDb, id: i16) -> BTreeMap<NaiveDate, Decimal> {
    levels_of(db, id)
        .await
        .into_iter()
        .filter_map(|r| r.1.map(|l| (r.0, l)))
        .collect()
}

fn window() -> DateWindow {
    DateWindow::new(day_n(1), day_n(3)).unwrap()
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn index_repair_rewrites_the_window_reports_it_and_reruns_identically() {
    let _g = LOCK.lock().await;
    let db = db();
    let r = repair_bed(&db, true).await;
    let source = nse(&r, &REAL[1..], 1, &[]);
    let report =
        index_repair::repair_index_snapshots(&db, &source, window(), Some(&r.slugs), false)
            .await
            .unwrap();
    assert!(report.succeeded());
    assert_eq!((report.trading_days, report.days_done), (3, 3));
    assert!(report.refused.is_empty());
    let fifty = index_levels(&db, r.ids[0]).await;
    assert_eq!(fifty[&day_n(1)], dec("24476.10"));
    assert_eq!(fifty[&day_n(3)], dec("24388.15"));
    assert_eq!(
        fifty[&day_n(0)],
        dec("24398.70"),
        "the anchor is outside the window"
    );
    assert_eq!(
        index_levels(&db, r.ids[1]).await[&day_n(2)],
        dec("23490.90")
    );
    let text = index_repair::format_report(&report);
    assert!(text.contains(&format!("anchor {}: 24398.7000 on 2026-07-07", r.slugs[0])));
    assert!(text.contains(&format!(
        "2026-07-08 {}: 1004.6026 -> 24476.1000",
        r.slugs[0]
    )));
    assert!(text.contains(&format!(
        "2026-07-10 {}: 1606.5549 -> 23401.0500",
        r.slugs[1]
    )));
    assert!(text.contains("3 trading day(s)"));
    assert!(text.contains("6 reported level(s) changed"));

    let before = (
        index_levels(&db, r.ids[0]).await,
        index_levels(&db, r.ids[1]).await,
    );
    let again = index_repair::repair_index_snapshots(&db, &source, window(), Some(&r.slugs), false)
        .await
        .unwrap();
    assert!(again.succeeded());
    assert_eq!(again.rewritten(), 0);
    let after = (
        index_levels(&db, r.ids[0]).await,
        index_levels(&db, r.ids[1]).await,
    );
    assert_eq!(before, after, "house rule 7");

    let weekend = nse(&r, &REAL[1..], 1, &[]);
    index_repair::repair_index_snapshots(
        &db,
        &weekend,
        DateWindow::new(day_n(1), day_n(5)).unwrap(),
        Some(&r.slugs),
        false,
    )
    .await
    .unwrap();
    assert_eq!(
        *weekend.asked.lock().unwrap(),
        vec![day_n(1), day_n(2), day_n(3)],
        "ascending, trading days only"
    );
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn index_repair_refuses_what_fails_the_guard_and_survives_a_failed_day() {
    let _g = LOCK.lock().await;
    let db = db();
    let r = repair_bed(&db, false).await;
    let report = index_repair::repair_index_snapshots(
        &db,
        &nse(&r, &FIXTURE, 1, &[]),
        window(),
        Some(&r.slugs),
        false,
    )
    .await
    .unwrap();
    assert_eq!((report.days_done, report.rows_written), (3, 0));
    assert_eq!(report.refused.len(), 6);
    assert!(
        report.refused[0].starts_with(&format!(
            "{} 2026-07-08: level 1004.6026 is -95.9%",
            r.slugs[0]
        )),
        "{}",
        report.refused[0]
    );
    assert_eq!(
        index_levels(&db, r.ids[0])
            .await
            .into_keys()
            .collect::<Vec<_>>(),
        vec![day_n(0)]
    );

    let r = repair_bed(&db, true).await;
    let report = index_repair::repair_index_snapshots(
        &db,
        &nse(&r, &REAL[1..], 1, &[day_n(2)]),
        window(),
        Some(&r.slugs),
        false,
    )
    .await
    .unwrap();
    assert!(!report.succeeded());
    assert_eq!(report.failures.len(), 1);
    assert_eq!(report.failures[0].0, "2026-07-09");
    assert!(report.failures[0].1.contains("404"));
    let fifty = index_levels(&db, r.ids[0]).await;
    assert_eq!(fifty[&day_n(1)], dec("24476.10"));
    assert_eq!(fifty[&day_n(2)], dec("1022.5254"));
    assert_eq!(fifty[&day_n(3)], dec("1014.1944"));
    assert!(
        report
            .refused
            .iter()
            .any(|m| m.starts_with(&format!("{} 2026-07-10: level 24388.15", r.slugs[0])))
    );
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn index_repair_narrows_by_slug_and_a_dry_run_writes_nothing() {
    let _g = LOCK.lock().await;
    let db = db();
    let r = repair_bed(&db, true).await;
    index_repair::repair_index_snapshots(
        &db,
        &nse(&r, &REAL[1..], 1, &[]),
        window(),
        Some(&r.slugs[1..]),
        false,
    )
    .await
    .unwrap();
    assert_eq!(
        index_levels(&db, r.ids[1]).await[&day_n(1)],
        dec("23440.25")
    );
    assert_eq!(
        index_levels(&db, r.ids[0]).await[&day_n(1)],
        dec("1004.6026")
    );

    let r = repair_bed(&db, true).await;
    let report = index_repair::repair_index_snapshots(
        &db,
        &nse(&r, &REAL[1..], 1, &[]),
        window(),
        Some(&r.slugs),
        true,
    )
    .await
    .unwrap();
    assert_eq!(report.rewritten(), 2);
    assert!(index_repair::format_report(&report).contains(&format!(
        "2026-07-08 {}: 1004.6026 -> 24476.1000",
        r.slugs[0]
    )));
    assert_eq!(report.refused.len(), 4);
    assert_eq!(
        index_levels(&db, r.ids[0]).await[&day_n(1)],
        dec("1004.6026")
    );
    db.close().await;
}

// ---------------------------------------------------------------------------------------------
// eq_bars
// ---------------------------------------------------------------------------------------------

fn minute(day: NaiveDate, h: u32, m: u32) -> DateTime<FixedOffset> {
    day.and_time(NaiveTime::from_hms_opt(h, m, 0).unwrap())
        .and_local_timezone(ist())
        .unwrap()
}

fn mbar(ts: DateTime<FixedOffset>, price: &str) -> MinuteBarRecord {
    let v = dec(price);
    MinuteBarRecord::new(MinuteBarFields {
        ts: PyDateTime::Aware(ts),
        open: v,
        high: v + dec("0.005"),
        low: v - Decimal::ONE,
        close: v,
        volume: 100,
    })
    .unwrap()
}

struct Reader {
    bars: Vec<MinuteBarRecord>,
    failing: Option<i64>,
    calls: Mutex<Vec<(i64, MinuteBound, MinuteBound)>>,
}

fn reader(bars: Vec<MinuteBarRecord>) -> Reader {
    Reader {
        bars,
        failing: None,
        calls: Mutex::new(Vec::new()),
    }
}

fn bound(b: MinuteBound, end: bool) -> DateTime<FixedOffset> {
    match b {
        MinuteBound::Date(day) => {
            let at = if end {
                NaiveTime::from_hms_opt(23, 59, 59).unwrap()
            } else {
                NaiveTime::MIN
            };
            day.and_time(at).and_local_timezone(ist()).unwrap()
        }
        MinuteBound::DateTime(PyDateTime::Aware(x)) => x,
        MinuteBound::DateTime(PyDateTime::Naive(x)) => x.and_local_timezone(ist()).unwrap(),
    }
}

impl MinuteReader for Reader {
    fn minute_bars(
        &self,
        token: i64,
        lo: MinuteBound,
        hi: MinuteBound,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<MinuteBarRecord>, ProviderError>> + Send + '_>>
    {
        Box::pin(async move {
            self.calls.lock().unwrap().push((token, lo, hi));
            if self.failing == Some(token) {
                return Err(ProviderError::upstream_unavailable("Kite: token not found"));
            }
            let (lo, hi) = (bound(lo, false), bound(hi, true));
            Ok(self
                .bars
                .iter()
                .filter(|b| b.ts.aware().is_some_and(|ts| lo <= ts && ts <= hi))
                .cloned()
                .collect())
        })
    }
}

async fn stored_minutes(db: &WriteDb, id: i64) -> Vec<(DateTime<FixedOffset>, Decimal)> {
    let mut tx = db.begin().await.unwrap();
    let rows: Vec<(DateTime<chrono::Utc>, Decimal)> =
        sqlx::query_as("SELECT ts, close FROM eq_minute_bar WHERE instrument_id = $1 ORDER BY ts")
            .bind(id)
            .fetch_all(&mut *tx)
            .await
            .unwrap();
    tx.rollback().await.unwrap();
    rows.into_iter()
        .map(|(ts, c)| (ts.with_timezone(&ist()), c))
        .collect()
}

async fn name(db: &WriteDb, token: i64) -> EqName {
    let symbol = tag();
    EqName {
        instrument_id: make_instrument(db, &symbol, "EQ", Some(token)).await,
        symbol,
        kite_token: token,
    }
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn the_session_reconcile_is_idempotent_closed_minutes_only_and_rounded() {
    let _g = LOCK.lock().await;
    let db = db();
    let day = d(2026, 9, 25);
    let a = name(&db, 7_500_001).await;
    let source = reader(vec![
        mbar(minute(day, 9, 15), "100.004"),
        mbar(minute(day, 9, 16), "100.005"),
        mbar(minute(day, 15, 29), "99"),
    ]);
    let names = [a.clone()];
    let now = minute(day, 15, 30);
    let first = eq_bars::reconcile_session(&db, &source, day, now, &names)
        .await
        .unwrap();
    let snapshot = stored_minutes(&db, a.instrument_id).await;
    let again = eq_bars::reconcile_session(&db, &source, day, now, &names)
        .await
        .unwrap();
    assert_eq!(
        (first.written, again.written, first.calls, again.calls),
        (3, 3, 1, 1)
    );
    assert_eq!(
        stored_minutes(&db, a.instrument_id).await,
        snapshot,
        "house rule 7"
    );
    assert_eq!(
        snapshot,
        vec![
            (minute(day, 9, 15), dec("100.00")),
            (minute(day, 9, 16), dec("100.01")),
            (minute(day, 15, 29), dec("99.00")),
        ]
    );
    let calls = source.calls.lock().unwrap().clone();
    assert_eq!(
        calls[0],
        (
            7_500_001,
            MinuteBound::from(minute(day, 9, 15)),
            MinuteBound::from(now)
        )
    );

    let b = name(&db, 7_500_002).await;
    let forming = reader(vec![
        mbar(minute(day, 9, 15), "10"),
        mbar(minute(day, 9, 16), "11"),
    ]);
    let names = [b.clone()];
    eq_bars::reconcile_session(
        &db,
        &forming,
        day,
        minute(day, 9, 16) + Duration::seconds(20),
        &names,
    )
    .await
    .unwrap();
    assert_eq!(stored_minutes(&db, b.instrument_id).await.len(), 1);
    eq_bars::reconcile_session(&db, &forming, day, minute(day, 9, 17), &names)
        .await
        .unwrap();
    assert_eq!(stored_minutes(&db, b.instrument_id).await.len(), 2);

    let good = name(&db, 7_500_003).await;
    let bad = name(&db, 7_500_004).await;
    let flaky = Reader {
        failing: Some(7_500_004),
        ..reader(vec![mbar(minute(day, 9, 15), "5")])
    };
    let report = eq_bars::reconcile_session(&db, &flaky, day, now, &[good, bad.clone()])
        .await
        .unwrap();
    assert_eq!(report.written, 1);
    assert_eq!(report.skipped.len(), 1);
    assert_eq!(report.skipped[0].0, bad.symbol);
    assert!(
        report.skipped[0].1.ends_with("Kite: token not found"),
        "{:?}",
        report.skipped
    );
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn the_minute_backfill_resumes_from_the_newest_stored_bar() {
    let _g = LOCK.lock().await;
    let db = db();
    let c = name(&db, 7_500_005).await;
    let (jan, feb, mar) = (d(2026, 1, 5), d(2026, 2, 5), d(2026, 3, 5));
    let source = reader(vec![
        mbar(minute(jan, 10, 0), "1"),
        mbar(minute(feb, 10, 0), "2"),
        mbar(minute(mar, 10, 0), "3"),
    ]);
    let mut tx = db.begin().await.unwrap();
    eq_bars::upsert_bars(
        &mut tx,
        c.instrument_id,
        vec![mbar(minute(jan, 10, 0), "1")],
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let report = eq_bars::backfill(
        &db,
        &source,
        d(2026, 1, 1),
        d(2026, 3, 31),
        minute(d(2026, 3, 31), 16, 0),
        std::slice::from_ref(&c),
    )
    .await
    .unwrap();
    assert_eq!(
        report.resumed_from,
        vec![(c.symbol.clone(), "2026-01-05".to_owned())]
    );
    assert_eq!(source.calls.lock().unwrap()[0].1, MinuteBound::Date(jan));
    assert!(report.calls >= 1);
    let closes: Vec<Decimal> = stored_minutes(&db, c.instrument_id)
        .await
        .into_iter()
        .map(|(_, c)| c)
        .collect();
    assert_eq!(closes, vec![dec("1.00"), dec("2.00"), dec("3.00")]);
    let err = eq_bars::backfill(
        &db,
        &source,
        d(2026, 3, 1),
        d(2026, 1, 1),
        minute(jan, 0, 0),
        &[],
    )
    .await
    .unwrap_err();
    assert_eq!(err.python_type(), "ValueError");
    db.close().await;
}

#[tokio::test]
#[ignore = "needs BASKFY_RS_BACKFILLS_DB_URL (an rst_* clone of the bed): --ignored"]
async fn the_minute_universe_is_the_swing_books_liquid_universe_with_tokens() {
    let _g = LOCK.lock().await;
    let db = db();
    let mut tx = db.begin().await.unwrap();
    let as_of: NaiveDate = sqlx::query_scalar(
        "SELECT trade_date FROM pipeline_run WHERE data_version IS NOT NULL \
         ORDER BY data_version DESC LIMIT 1",
    )
    .fetch_one(&mut *tx)
    .await
    .unwrap();
    let names = eq_bars::liquid_names(&mut tx, as_of, -1).await.unwrap();
    tx.rollback().await.unwrap();
    assert!(!names.is_empty());
    let symbols: Vec<&str> = names.iter().map(|n| n.symbol.as_str()).collect();
    let mut sorted = symbols.clone();
    sorted.sort_unstable();
    assert_eq!(
        symbols, sorted,
        "sorted by symbol, as liquid_universe returns them"
    );
    assert!(names.iter().all(|n| n.kite_token > 0));
    db.close().await;
}
