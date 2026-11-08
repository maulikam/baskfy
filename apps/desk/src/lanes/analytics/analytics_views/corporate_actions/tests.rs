//! `tests/test_corporate_actions.py`, ported: validation, the two treatments, persistence.
//! The FIFO-rebuild cases (`build_lots`, `rebuild_symbols`) belong with the tradebook port;
//! here the book is driven through [`apply_to_book`] directly.

#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use super::*;

fn d(y: i32, m: u32, day: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, day).unwrap()
}

fn today() -> NaiveDate {
    d(2026, 10, 30)
}

fn bonus_at(ex: &str, new: &str, old: &str) -> Action {
    parse("X", "bonus", ex, new, old, "", today()).unwrap()
}

fn split_of(new: &str, old: &str) -> Action {
    parse("X", "split", "2026-06-01", new, old, "", today()).unwrap()
}

#[derive(Debug, Clone, PartialEq)]
struct Lot {
    when: NaiveDateTime,
    quantity: i64,
    price: f64,
}

impl BookLot for Lot {
    fn bonus_lot(_symbol: &str, when: NaiveDateTime, quantity: i64) -> Self {
        Self {
            when,
            quantity,
            price: 0.0,
        }
    }
    fn quantity(&self) -> i64 {
        self.quantity
    }
    fn set_quantity(&mut self, quantity: i64) {
        self.quantity = quantity;
    }
    fn price(&self) -> f64 {
        self.price
    }
    fn set_price(&mut self, price: f64) {
        self.price = price;
    }
}

fn lot(date: NaiveDate, quantity: i64, price: f64) -> Lot {
    Lot {
        when: date.and_hms_opt(10, 0, 0).unwrap(),
        quantity,
        price,
    }
}

fn err(r: Result<Action, CorporateActionError>) -> String {
    r.err().map(|e| e.0).unwrap_or_default()
}

#[test]
fn an_unknown_kind_is_refused() {
    let e = err(parse("X", "merger", "2026-06-01", "1", "1", "", today()));
    assert_eq!(e, "kind must be one of bonus, split, got 'merger'");
}

#[test]
fn a_bad_date_is_refused() {
    let e = err(parse("X", "bonus", "June 2026", "1", "1", "", today()));
    assert!(e.contains("not a YYYY-MM-DD"), "{e}");
    assert_eq!(e, "ex_date 'June 2026' is not a YYYY-MM-DD date");
}

#[test]
fn a_future_ex_date_is_refused() {
    let ahead = (today() + chrono::Duration::days(5))
        .format("%Y-%m-%d")
        .to_string();
    assert!(err(parse("X", "bonus", &ahead, "1", "1", "", today())).contains("in the future"));
}

#[test]
fn a_non_positive_ratio_is_refused() {
    for (new, old) in [("0", "1"), ("1", "0"), ("-1", "1")] {
        assert!(err(parse("X", "bonus", "2026-06-01", new, old, "", today())).contains("positive"));
    }
}

#[test]
fn a_non_numeric_ratio_is_refused() {
    assert_eq!(
        err(parse("X", "bonus", "2026-06-01", "two", "1", "", today())),
        "ratio must be numeric"
    );
}

#[test]
fn a_split_that_does_not_increase_the_count_is_refused() {
    assert!(
        err(parse("X", "split", "2026-06-01", "1", "2", "", today())).contains("must increase")
    );
}

#[test]
fn the_symbol_is_normalised() {
    let a = parse(" anandrathi ", "bonus", "2026-06-01", "1", "1", "", today()).unwrap();
    assert_eq!(a.symbol, "ANANDRATHI");
}

#[test]
fn a_bonus_adds_shares_at_nil_cost() {
    let mut book = vec![lot(d(2026, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &bonus_at("2026-06-01", "1", "1"));
    assert_eq!(book.iter().map(|l| l.quantity).sum::<i64>(), 200);
    let nil: Vec<&Lot> = book.iter().filter(|l| l.price == 0.0).collect();
    assert_eq!(nil.len(), 1);
    assert_eq!(nil[0].quantity, 100);
}

#[test]
fn a_bonus_lot_is_dated_at_the_ex_date_not_the_original_purchase() {
    let mut book = vec![lot(d(2024, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &bonus_at("2026-06-01", "1", "1"));
    let nil = book.iter().find(|l| l.price == 0.0).unwrap();
    assert_eq!(nil.when.date(), d(2026, 6, 1));
}

#[test]
fn a_bonus_leaves_the_original_lot_untouched() {
    let mut book = vec![lot(d(2026, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &bonus_at("2026-06-01", "1", "1"));
    let orig = book.iter().find(|l| l.price == 500.0).unwrap();
    assert_eq!((orig.quantity, orig.when.date()), (100, d(2026, 1, 5)));
}

#[test]
fn a_two_for_one_bonus_triples_the_holding() {
    let mut book = vec![lot(d(2026, 1, 5), 50, 100.0)];
    apply_to_book(&mut book, &bonus_at("2026-06-01", "2", "1"));
    assert_eq!(book.iter().map(|l| l.quantity).sum::<i64>(), 150);
}

#[test]
fn a_split_multiplies_quantity_and_divides_cost() {
    let mut book = vec![lot(d(2026, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &split_of("2", "1"));
    assert_eq!(book.len(), 1);
    assert_eq!(book[0].quantity, 200);
    assert!((book[0].price - 250.0).abs() < 1e-9);
}

#[test]
fn a_split_preserves_the_original_acquisition_date() {
    let mut book = vec![lot(d(2024, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &split_of("2", "1"));
    assert_eq!(book[0].when.date(), d(2024, 1, 5));
}

#[test]
fn a_split_conserves_the_total_cost_basis() {
    let mut book = vec![lot(d(2026, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &split_of("5", "1"));
    #[allow(clippy::cast_precision_loss)]
    let basis = book[0].quantity as f64 * book[0].price;
    assert!((basis - 100.0 * 500.0).abs() < 1e-6);
}

#[test]
fn a_split_creates_no_nil_cost_lot() {
    let mut book = vec![lot(d(2026, 1, 5), 100, 500.0)];
    apply_to_book(&mut book, &split_of("2", "1"));
    assert!(!book.iter().any(|l| l.price == 0.0));
}

#[test]
fn a_flat_book_gets_no_bonus() {
    let mut book = vec![lot(d(2026, 1, 5), 0, 500.0)];
    apply_to_book(&mut book, &bonus_at("2026-06-01", "1", "1"));
    assert_eq!(book.len(), 1);
}

#[test]
fn the_description_states_the_tax_treatment() {
    let b = bonus_at("2026-06-01", "1", "1").describe();
    assert_eq!(
        b,
        "1:1 bonus — 1 new share(s) per share held, nil cost, holding period starts 2026-06-01"
    );
    let s = split_of("2", "1").describe();
    assert!(s.contains("inherited"));
    assert_eq!(
        s,
        "2:1 split — each share becomes 2, cost per share divided, holding period inherited"
    );
}

async fn conn() -> (tempfile::TempDir, Conn) {
    let dir = tempfile::tempdir().unwrap();
    let mut c = crate::storage::Db::connect_sqlite_path(&dir.path().join("p.db"))
        .await
        .unwrap();
    c.migrate().await.unwrap();
    (dir, c)
}

#[tokio::test]
async fn recording_the_same_action_twice_does_not_double_it() {
    let (_dir, mut c) = conn().await;
    let a = record(
        &mut c,
        &bonus_at("2026-06-01", "1", "1"),
        "2026-10-30T10:00:00",
    )
    .await
    .unwrap();
    let b = record(
        &mut c,
        &bonus_at("2026-06-01", "1", "1"),
        "2026-10-30T10:00:01",
    )
    .await
    .unwrap();
    assert_eq!(a, b);
    assert_eq!(load(&mut c, None).await.unwrap().len(), 1);
}

#[tokio::test]
async fn an_action_only_loads_for_its_own_symbol() {
    let (_dir, mut c) = conn().await;
    record(
        &mut c,
        &bonus_at("2026-06-01", "1", "1"),
        "2026-10-30T10:00:00",
    )
    .await
    .unwrap();
    let y = load(&mut c, Some(&["Y".to_owned()])).await.unwrap();
    assert!(y.is_empty());
    assert!(load(&mut c, Some(&[])).await.unwrap().is_empty());
    assert_eq!(
        load(&mut c, Some(&["X".to_owned()])).await.unwrap().len(),
        1
    );
}

#[tokio::test]
async fn removing_an_action_deletes_it_once() {
    let (_dir, mut c) = conn().await;
    let id = record(
        &mut c,
        &bonus_at("2026-06-01", "1", "1"),
        "2026-10-30T10:00:00",
    )
    .await
    .unwrap();
    assert!(remove(&mut c, id).await.unwrap());
    assert!(!remove(&mut c, id).await.unwrap());
    assert!(listing(&mut c).await.unwrap().is_empty());
}

#[tokio::test]
async fn the_listing_carries_the_description_newest_first() {
    let (_dir, mut c) = conn().await;
    record(
        &mut c,
        &bonus_at("2026-01-01", "1", "1"),
        "2026-10-30T10:00:00",
    )
    .await
    .unwrap();
    record(&mut c, &split_of("2", "1"), "2026-10-30T10:00:00")
        .await
        .unwrap();
    let l = listing(&mut c).await.unwrap();
    assert_eq!(l.len(), 2);
    assert_eq!(l[0].get("kind"), Some(&PyValue::Str("split".to_owned())));
    assert!(
        l[1].get("describe")
            .is_some_and(|v| v.py_str().contains("nil cost"))
    );
}

/// `dt.date.fromisoformat` as Python 3.12 reads an ex-date, including the ISO week forms the
/// form would otherwise refuse while Python accepts them. Expected values printed by the
/// desk venv's Python 3.12.
#[test]
fn ex_dates_parse_as_python_312_parses_them() {
    let table: [(&str, Option<&str>); 26] = [
        ("2026-W01-1", Some("2025-12-29")),
        ("2026W011", Some("2025-12-29")),
        ("2026-W01", Some("2025-12-29")),
        ("2026W01", Some("2025-12-29")),
        ("2026-W53-1", Some("2026-12-28")),
        ("2020-W53-7", Some("2021-01-03")),
        ("2026-W00-1", None),
        ("2026-W01-8", None),
        ("2026-W01-0", None),
        ("20260101", Some("2026-01-01")),
        ("2026-01-01", Some("2026-01-01")),
        ("2026-1-01", None),
        ("2026-02-29", None),
        ("2024-02-29", Some("2024-02-29")),
        ("0000-01-01", None),
        ("0001-01-01", Some("0001-01-01")),
        ("2026-01-01T00:00", None),
        ("2026-001", None),
        ("2026W0110", None),
        ("2026-W011", None),
        ("2026-W1-1", None),
        ("2026/01/01", None),
        ("abcd-ef-gh", None),
        ("2026-13-01", None),
        ("", None),
        ("2026", None),
    ];
    for (raw, want) in table {
        let got = fromisoformat(raw).map(|d| d.format("%Y-%m-%d").to_string());
        assert_eq!(got.as_deref(), want, "{raw:?}");
    }
}
