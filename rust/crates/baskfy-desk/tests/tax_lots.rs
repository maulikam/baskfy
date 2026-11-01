//! `tests/test_regime_plan.py`'s FIFO tax-lot section, against the desk's `tax_lots` port.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use baskfy_desk::lanes::weekly::weekly_books::tax_lots::{
    self as tl, DEFAULT_REVIEW_DAYS, LONG_TERM_DAYS, Lot, TAX_LOT_LONG_TERM, TAX_LOT_LOSS,
    TAX_LOT_NEAR_LTCG, TAX_LOTS_INCOMPLETE, TAX_MISSING_ACQUISITION_DATE,
    TAX_MISSING_ACQUISITION_PRICE, TaxReview,
};
use baskfy_desk::storage::{Conn, Db, Param};
use baskfy_desk::wire::{PyValue, dict};
use chrono::{Duration, NaiveDate};

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn as_of() -> NaiveDate {
    d(2026, 6, 5)
}

fn opt(v: Option<&str>) -> PyValue {
    v.map_or(PyValue::None, |s| PyValue::Str(s.to_owned()))
}

fn lots(specs: &[(i64, Option<&str>, Option<f64>)]) -> Vec<Lot> {
    let rows: Vec<_> = specs
        .iter()
        .map(|(q, on, p)| {
            dict([
                ("quantity", PyValue::Int(*q)),
                ("acquired_on", opt(*on)),
                ("price", p.map_or(PyValue::None, PyValue::Float)),
            ])
        })
        .collect();
    tl::lots_from_rows("X", &rows).unwrap()
}

fn lot(q: i64, on: &str, p: f64) -> Vec<Lot> {
    lots(&[(q, Some(on), Some(p))])
}

fn review(qty: i64, l: &[Lot]) -> TaxReview {
    tl::review_sale("X", qty, 100.0, l, as_of(), DEFAULT_REVIEW_DAYS)
}

#[test]
fn proposed_quantity_consumes_fifo_lots_in_order() {
    let ls = lots(&[
        (100, Some("2025-01-01"), Some(50.0)),
        (100, Some("2025-06-01"), Some(60.0)),
        (100, Some("2026-01-01"), Some(70.0)),
    ]);
    let rev = review(150, &ls);
    let q: Vec<i64> = rev.consumptions.iter().map(|c| c.quantity).collect();
    assert_eq!(q, [100, 50]);
    let on: Vec<Option<NaiveDate>> = rev.consumptions.iter().map(|c| c.lot.acquired_on).collect();
    assert_eq!(on, [Some(d(2025, 1, 1)), Some(d(2025, 6, 1))]);
    assert_eq!(rev.covered_qty, 150);
}

#[test]
fn estimated_gain_uses_the_consumed_lots_only() {
    let ls = lots(&[
        (100, Some("2025-01-01"), Some(50.0)),
        (100, Some("2026-01-01"), Some(90.0)),
    ]);
    assert_eq!(review(100, &ls).estimated_gain, Some(100.0 * 50.0));
}

#[test]
fn position_level_age_would_be_wrong() {
    let ls = lots(&[
        (100, Some("2024-01-01"), Some(50.0)),
        (100, Some("2025-07-20"), Some(90.0)),
    ]);
    let small = review(100, &ls);
    assert!(!small.flagged);
    assert!(small.codes.contains(&TAX_LOT_LONG_TERM));
    assert!(review(200, &ls).flagged);
}

#[test]
fn lot_is_flagged_inside_the_review_window() {
    let rev = review(100, &lot(100, "2025-07-01", 50.0));
    assert!(rev.flagged && rev.codes.contains(&TAX_LOT_NEAR_LTCG));
    assert_eq!(rev.flagged_qty, 100);
}

#[test]
fn lot_outside_the_review_window_is_not_flagged() {
    assert!(!review(100, &lot(100, "2025-11-01", 50.0)).flagged);
}

#[test]
fn boundary_of_the_review_window_is_inclusive() {
    let acq = as_of() - Duration::days(LONG_TERM_DAYS - 45);
    let rev = tl::review_sale(
        "X",
        100,
        100.0,
        &lot(100, &acq.format("%Y-%m-%d").to_string(), 50.0),
        as_of(),
        45,
    );
    assert!(rev.flagged);
}

#[test]
fn a_losing_lot_is_never_ltcg_deferred() {
    let rev = review(100, &lot(100, "2025-07-01", 150.0));
    assert!(!rev.flagged);
    assert!(rev.codes.contains(&TAX_LOT_LOSS));
}

#[test]
fn already_long_term_lot_is_not_flagged() {
    let rev = review(100, &lot(100, "2024-01-01", 50.0));
    assert!(!rev.flagged && rev.codes.contains(&TAX_LOT_LONG_TERM));
}

#[test]
fn missing_acquisition_date_yields_tax_data_unknown() {
    let rev = review(100, &lots(&[(100, None, Some(50.0))]));
    assert!(rev.data_unknown);
    assert!(rev.codes.contains(&TAX_MISSING_ACQUISITION_DATE));
    assert!(rev.manual_review());
}

#[test]
fn missing_acquisition_price_is_distinguished_from_missing_date() {
    let rev = review(100, &lots(&[(100, Some("2025-07-01"), None)]));
    assert!(rev.codes.contains(&TAX_MISSING_ACQUISITION_PRICE));
    assert!(!rev.codes.contains(&TAX_MISSING_ACQUISITION_DATE));
    assert_eq!(rev.estimated_gain, None);
}

#[test]
fn incomplete_lot_history_is_never_assumed_tax_safe() {
    let rev = review(200, &lot(50, "2025-01-01", 50.0));
    assert_eq!(rev.covered_qty, 50);
    assert!(rev.codes.contains(&TAX_LOTS_INCOMPLETE));
    assert!(rev.data_unknown && rev.manual_review());
}

#[test]
fn no_lots_at_all_is_unknown_not_safe() {
    let rev = review(100, &[]);
    assert!(rev.data_unknown);
    assert_eq!(rev.covered_qty, 0);
}

#[test]
fn manual_review_items_carry_lot_detail() {
    let reviews = vec![("X".to_owned(), review(100, &lot(100, "2025-07-01", 50.0)))];
    let items = tl::manual_review_items(&reviews);
    assert_eq!(items.len(), 1);
    let Some(PyValue::List(detail)) = items[0].get("detail") else {
        panic!("{:?}", items[0])
    };
    let PyValue::Map(first) = &detail[0] else {
        panic!()
    };
    assert!(!matches!(
        first.get("days_to_ltcg"),
        None | Some(PyValue::None)
    ));
    assert_eq!(
        items[0].get("estimated_gain"),
        Some(&PyValue::Float(5000.0))
    );
}

async fn sqlite() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    conn.migrate().await.unwrap();
    (dir, conn)
}

fn ts(y: i32, m: u32, day: u32) -> f64 {
    let local = d(y, m, day).and_hms_opt(0, 0, 0).unwrap();
    let t = local.and_local_timezone(chrono::Local).single().unwrap();
    t.timestamp() as f64
}

async fn trade(c: &mut Conn, sym: &str, qty: i64, entry: f64, price: f64, exit: Option<f64>) {
    c.execute(
        "INSERT INTO trades(symbol, qty, entry_ts, entry_price, exit_ts) VALUES(?,?,?,?,?)",
        &[
            Param::from(sym),
            Param::from(qty),
            Param::from(entry),
            Param::from(price),
            Param::from(exit),
        ],
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn open_lots_read_from_the_trades_table() {
    let (_d, mut c) = sqlite().await;
    trade(&mut c, "X", 100, ts(2025, 7, 1), 50.0, None).await;
    trade(&mut c, "X", 50, ts(2024, 1, 1), 40.0, Some(ts(2025, 1, 1))).await;
    let ls = tl::open_lots(&mut c, "X").await.unwrap();
    assert_eq!(ls.len(), 1);
    assert_eq!(ls[0].quantity, 100);
    assert_eq!(ls[0].acquired_on, Some(d(2025, 7, 1)));
}

#[tokio::test]
async fn review_sales_batches_by_symbol() {
    let (_d, mut c) = sqlite().await;
    for (sym, px) in [("A", 50.0), ("B", 40.0)] {
        trade(&mut c, sym, 100, ts(2025, 7, 1), px, None).await;
    }
    let sells = vec![
        ("A".to_owned(), (100, 100.0)),
        ("B".to_owned(), (100, 100.0)),
    ];
    let out = tl::review_sales(&mut c, &sells, as_of(), DEFAULT_REVIEW_DAYS)
        .await
        .unwrap();
    let names: Vec<&str> = out.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(names, ["A", "B"]);
    assert_eq!(tl::flagged_symbols(&out), ["A", "B"]);
}
