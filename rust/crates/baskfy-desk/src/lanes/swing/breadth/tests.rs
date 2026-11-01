//! `tests/test_breadth_and_shadow.py`, the breadth half ("which number the cash bands see"),
//! ported test for test: `breadth_for_plan` over a fixed reader, as Python monkeypatches
//! `pipeline_breadth`. The band-table, flag and shadow-harness tests in that file belong to the
//! weekly lanes (`C.CASH_BANDS`, `cash_pct_for`, `SCAN_SOURCE_DEFAULT`, `docs/SHADOW-MODE.md`).
//!
//! Plus the reader's failure class against psycopg's: `exc.__class__.__name__` is written on the
//! plan ("pipeline unreachable (ProgrammingError)"), so a malformed `SCREENER_DATABASE_URL` must
//! be named as psycopg names it. The expected classes were recorded from psycopg 3 over libpq
//! 18.0 (`psycopg.connect(dsn, connect_timeout=2)`) with every reachable DSN pointed at
//! 127.0.0.1:18799, a port in this lane's range with nothing listening.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::NaiveDate;

use super::*;

struct Fixed(Result<(Option<f64>, BTreeSet<String>), PipelineError>);

#[async_trait]
impl PipelineBreadth for Fixed {
    async fn read(
        &self,
        _on: NaiveDate,
        _slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError> {
        self.0.clone()
    }
}

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

fn get(m: &PyDict, k: &str) -> PyValue {
    m.get(k).cloned().unwrap_or(PyValue::None)
}

fn s(m: &PyDict, k: &str) -> String {
    match get(m, k) {
        PyValue::Str(s) => s,
        other => panic!("{k}: {other:?}"),
    }
}

const UNIVERSE: &str = "nifty-total-market";

fn day(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

#[tokio::test]
async fn test_the_pipeline_wins_when_the_population_is_the_same() {
    let reader = Fixed(Ok((Some(68.6347), set(&["A", "B"]))));
    let result =
        breadth_for_plan(68.6, &set(&["A", "B"]), day(2026, 8, 18), UNIVERSE, &reader).await;
    assert_eq!(s(&result, "source"), "pipeline");
    assert_eq!(get(&result, "value"), PyValue::Float(68.6347));
    assert_eq!(
        get(&result, "scan_value"),
        PyValue::Float(68.6),
        "both are kept, always"
    );
    assert!(s(&result, "reason").contains("same 2 symbols"));
}

#[tokio::test]
async fn test_a_different_population_falls_back_to_the_scans_own() {
    let reader = Fixed(Ok((Some(12.0), set(&["X", "Y"]))));
    let result =
        breadth_for_plan(68.6, &set(&["A", "B"]), day(2026, 8, 18), UNIVERSE, &reader).await;
    assert_eq!(s(&result, "source"), "scan");
    assert_eq!(get(&result, "value"), PyValue::Float(68.6));
    assert_eq!(
        get(&result, "pipeline_value"),
        PyValue::Float(12.0),
        "recorded, and deliberately not used"
    );
    assert!(s(&result, "reason").contains("differs from"));
}

#[tokio::test]
async fn test_a_missing_pipeline_row_falls_back() {
    let reader = Fixed(Ok((None, set(&["A"]))));
    let result = breadth_for_plan(68.6, &set(&["A"]), day(2020, 1, 2), UNIVERSE, &reader).await;
    assert_eq!(s(&result, "source"), "scan");
    assert!(s(&result, "reason").contains("no market_health_daily row"));
}

#[tokio::test]
async fn test_an_unreachable_screener_never_stops_a_rebalance() {
    // `raise OSError("connection refused")` → `exc.__class__.__name__` is "OSError".
    let reader = Fixed(Err(PipelineError("OSError".to_owned())));
    let result = breadth_for_plan(68.6, &set(&["A"]), day(2026, 8, 18), UNIVERSE, &reader).await;
    assert_eq!(s(&result, "source"), "scan");
    assert_eq!(get(&result, "value"), PyValue::Float(68.6));
    assert!(s(&result, "reason").contains("unreachable"));
    assert_eq!(
        s(&result, "reason"),
        "pipeline unreachable (OSError); used the scan's own"
    );
}

#[tokio::test]
async fn test_both_numbers_are_always_recorded() {
    for symbols in [set(&["A"]), set(&["Z"])] {
        let reader = Fixed(Ok((Some(50.0), set(&["A"]))));
        let result = breadth_for_plan(68.6, &symbols, day(2026, 8, 18), UNIVERSE, &reader).await;
        assert_eq!(get(&result, "scan_value"), PyValue::Float(68.6));
        assert_eq!(get(&result, "pipeline_value"), PyValue::Float(50.0));
    }
}

/// psycopg's class for each DSN (recorded; see the module doc).
const PSYCOPG_CLASSES: [(&str, &str); 25] = [
    ("not a url", "ProgrammingError"),
    ("postgresql://u:p@127.0.0.1:notaport/db", "OperationalError"),
    ("postgresql://u:p@127.0.0.1:18799/db", "OperationalError"),
    (
        "postgres://u@127.0.0.1:18799/db?sslmode=bogus",
        "OperationalError",
    ),
    ("postgresql://%zz@127.0.0.1:18799/db", "ProgrammingError"),
    ("host=127.0.0.1 port=18799 dbname=x", "OperationalError"),
    (
        "postgresql://u@127.0.0.1:18799/db?bogus=1",
        "ProgrammingError",
    ),
    (
        "postgresql://u@127.0.0.1:18799/db?sslmode",
        "ProgrammingError",
    ),
    (
        "postgresql://u@127.0.0.1:18799/db?a=b=c",
        "ProgrammingError",
    ),
    ("postgresql://u@[::1:18799/db", "ProgrammingError"),
    ("postgresql://u@127.0.0.1:18799/d%00b", "ProgrammingError"),
    ("host='unterminated", "ProgrammingError"),
    ("host=127.0.0.1 port=18799 bogus=1", "ProgrammingError"),
    ("  host  =  127.0.0.1 port=18799", "OperationalError"),
    (
        "postgresql://u@127.0.0.1:18799/db?ssl=true",
        "OperationalError",
    ),
    (
        "postgresql://u@127.0.0.1:18799/db?application_name=x&target_session_attrs=any",
        "OperationalError",
    ),
    ("postgresql://u:p%40@127.0.0.1:18799/db", "OperationalError"),
    ("postgresql://u@127.0.0.1:18799/db?&", "ProgrammingError"),
    ("postgresql://u@127.0.0.1:18799/db?=x", "ProgrammingError"),
    (
        "host=127.0.0.1 port='18799' user='a b\\'c'",
        "OperationalError",
    ),
    ("host=127.0.0.1 port=notaport", "OperationalError"),
    (
        "host=127.0.0.1 port=18799 sslmode=bogus",
        "OperationalError",
    ),
    (
        "postgresql://u@127.0.0.1:18799,127.0.0.1:18798/db",
        "OperationalError",
    ),
    ("dbname", "ProgrammingError"),
    ("=x", "ProgrammingError"),
];

#[tokio::test]
async fn a_malformed_screener_url_is_named_as_psycopg_names_it() {
    for (dsn, class) in PSYCOPG_CLASSES {
        let reader = PgPipeline {
            dsn: dsn.to_owned(),
        };
        let got = reader.read(day(2026, 8, 18), UNIVERSE).await.unwrap_err();
        assert_eq!(got.0, class, "{dsn:?}");
        let result =
            breadth_for_plan(68.6, &set(&["A"]), day(2026, 8, 18), UNIVERSE, &reader).await;
        assert_eq!(
            s(&result, "reason"),
            format!("pipeline unreachable ({class}); used the scan's own")
        );
    }
}

#[test]
fn libpq_parses_the_key_value_form_sqlx_cannot() {
    let parsed = libpq_parse("host=127.0.0.1 port='18799' user='a b\\'c' dbname=baskfy").unwrap();
    assert_eq!(
        parsed,
        [
            ("host".to_owned(), "127.0.0.1".to_owned()),
            ("port".to_owned(), "18799".to_owned()),
            ("user".to_owned(), "a b'c".to_owned()),
            ("dbname".to_owned(), "baskfy".to_owned()),
        ]
    );
    let uri = libpq_parse("postgresql://u:p%40@h:5/db?sslmode=disable").unwrap();
    assert_eq!(
        uri,
        [
            ("user".to_owned(), "u".to_owned()),
            ("password".to_owned(), "p@".to_owned()),
            ("host".to_owned(), "h".to_owned()),
            ("port".to_owned(), "5".to_owned()),
            ("dbname".to_owned(), "db".to_owned()),
            ("sslmode".to_owned(), "disable".to_owned()),
        ]
    );
}

/// Driven by `tools/parity/breadth_diff.py`: the same rows on a bed database read by both
/// languages; every case's dict must equal Python's, key order and floats included.
#[tokio::test]
#[ignore = "needs the bed: run through tools/parity/breadth_diff.py"]
async fn pipeline_parity_on_the_bed() {
    // Read with the order-keeping JSON reader: Python's dict order is part of what is graded.
    let path = std::env::var("RSD_BREADTH_CASES").unwrap();
    let PyValue::List(cases) = PyValue::from_json(&std::fs::read(path).unwrap()).unwrap() else {
        panic!("the cases file is a list");
    };
    assert!(!cases.is_empty());
    for case in cases {
        let PyValue::Map(case) = case else {
            panic!("a case is an object")
        };
        let reader = PgPipeline {
            dsn: s(&case, "dsn"),
        };
        let symbols: BTreeSet<String> = match get(&case, "symbols") {
            PyValue::List(items) => items.iter().map(PyValue::py_str).collect(),
            other => panic!("{other:?}"),
        };
        let on = NaiveDate::parse_from_str(&s(&case, "on"), "%Y-%m-%d").unwrap();
        let PyValue::Float(scan) = get(&case, "scan") else {
            panic!("scan is a float")
        };
        let got = breadth_for_plan(scan, &symbols, on, UNIVERSE, &reader).await;
        let got = baskfy_providers::kite::py_json_dumps(&PyValue::Map(got)).unwrap();
        let want = baskfy_providers::kite::py_json_dumps(&get(&case, "python")).unwrap();
        assert_eq!(got, want, "{}", s(&case, "on"));
    }
}
