//! `tests/test_benchmark.py`: TRI CSV parsing, PRI/TRI coexistence, series labelling.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp,
    missing_docs
)]

use std::path::{Path, PathBuf};

use baskfy_desk::lanes::analytics::analytics_views::benchmark::{self as b, BenchError, PRI, TRI};
use baskfy_desk::storage::{Conn, Db, DbValue};
use chrono::NaiveDate;

async fn conn(dir: &Path) -> Conn {
    let mut c = Db::connect_sqlite_path(&dir.join("p.db")).await.unwrap();
    c.migrate().await.unwrap();
    c
}

fn csv(dir: &Path, name: &str, body: &str) -> PathBuf {
    let p = dir.join(name);
    std::fs::write(&p, body).unwrap();
    p
}

fn d(s: &str) -> NaiveDate {
    NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap()
}

fn pri(pairs: &[(&str, f64)]) -> Vec<(String, f64)> {
    b::series_pairs(&pairs.iter().map(|(t, v)| (d(t), *v)).collect::<Vec<_>>())
}

fn tri(pairs: &[(&str, f64)]) -> Vec<(String, f64)> {
    pairs.iter().map(|(t, v)| ((*t).to_owned(), *v)).collect()
}

fn f(v: Option<&DbValue>) -> Option<f64> {
    match v {
        Some(DbValue::Float(x)) => Some(*x),
        Some(DbValue::Int(i)) => Some(*i as f64),
        _ => None,
    }
}

fn i(v: Option<&DbValue>) -> i64 {
    match v {
        Some(DbValue::Int(x)) => *x,
        other => panic!("{other:?}"),
    }
}

// =============================================================================================
// TRI CSV parsing
// =============================================================================================

#[test]
fn tri_csv_standard_niftyindices_export() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "tri.csv",
        "Date,Total Returns Index,Net Total Return Index\n01 Jan 2026,\"12,345.67\",\"12,100.10\"\n02 Jan 2026,\"12,400.00\",\"12,150.00\"\n",
    );
    let rows = b::tri_csv_loader(&p).unwrap();
    assert_eq!(
        rows.iter().map(|r| r.date.as_str()).collect::<Vec<_>>(),
        ["2026-01-01", "2026-01-02"]
    );
    assert_eq!(
        rows.iter().map(|r| r.tri).collect::<Vec<_>>(),
        [12_345.67, 12_400.00]
    );
    assert_eq!(
        rows.iter().map(|r| r.net_tri).collect::<Vec<_>>(),
        [Some(12_100.10), Some(12_150.00)]
    );
}

#[test]
fn tri_csv_prefers_gross_over_net() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "tri.csv",
        "Date,Net Total Return Index,Total Returns Index\n01 Jan 2026,100.0,200.0\n",
    );
    assert_eq!(b::tri_csv_loader(&p).unwrap()[0].tri, 200.0);
}

#[test]
fn tri_csv_falls_back_to_net_when_gross_absent() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "tri.csv",
        "Date,Net Total Return Index\n01 Jan 2026,100.0\n",
    );
    assert_eq!(b::tri_csv_loader(&p).unwrap()[0].tri, 100.0);
}

#[test]
fn tri_csv_date_formats() {
    let dir = tempfile::tempdir().unwrap();
    for (raw, want) in [
        ("01 Jan 2026", "2026-01-01"),
        ("01-Jan-2026", "2026-01-01"),
        ("01/02/2026", "2026-02-01"),
        ("2026-01-01", "2026-01-01"),
        ("1 January 2026", "2026-01-01"),
    ] {
        let p = csv(
            dir.path(),
            "tri.csv",
            &format!("Date,Total Returns Index\n{raw},100.0\n"),
        );
        assert_eq!(b::tri_csv_loader(&p).unwrap()[0].date, want, "{raw}");
    }
}

#[test]
fn tri_csv_skips_unparseable_rows() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "tri.csv",
        "Date,Total Returns Index\n01 Jan 2026,100.0\n,\nnot a date,200.0\n02 Jan 2026,-\n03 Jan 2026,300.0\n",
    );
    let rows = b::tri_csv_loader(&p).unwrap();
    assert_eq!(
        rows.iter().map(|r| r.date.as_str()).collect::<Vec<_>>(),
        ["2026-01-01", "2026-01-03"]
    );
}

#[test]
fn tri_csv_deduplicates_and_sorts() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "tri.csv",
        "Date,Total Returns Index\n03 Jan 2026,300.0\n01 Jan 2026,100.0\n03 Jan 2026,999.0\n",
    );
    let rows = b::tri_csv_loader(&p).unwrap();
    assert_eq!(
        rows.iter().map(|r| r.date.as_str()).collect::<Vec<_>>(),
        ["2026-01-01", "2026-01-03"]
    );
    assert_eq!(rows[1].tri, 300.0);
}

#[test]
fn tri_csv_rejects_a_file_without_the_expected_columns() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(dir.path(), "bad.csv", "Date,Close\n01 Jan 2026,100.0\n");
    let e = b::tri_csv_loader(&p).unwrap_err();
    assert!(
        matches!(&e, BenchError::Value(m) if m.contains("Total Returns Index") && m.contains("['Date', 'Close']")),
        "{e}"
    );
}

#[test]
fn tri_csv_rejects_unquoted_thousands_separators() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(
        dir.path(),
        "unquoted.csv",
        "Date,Total Returns Index\n01 Jan 2026,12,345.67\n",
    );
    let e = b::tri_csv_loader(&p).unwrap_err();
    assert!(
        matches!(&e, BenchError::Value(m) if m.contains("more fields than the header") && m.contains("line 2")),
        "{e}"
    );
}

#[test]
fn tri_csv_rejects_an_empty_file() {
    let dir = tempfile::tempdir().unwrap();
    let p = csv(dir.path(), "empty.csv", "Date,Total Returns Index\n");
    assert!(
        matches!(b::tri_csv_loader(&p), Err(BenchError::Value(m)) if m.contains("no parseable rows"))
    );
    let blank = csv(dir.path(), "blank.csv", "");
    assert!(
        matches!(b::tri_csv_loader(&blank), Err(BenchError::Value(m)) if m.contains("empty or unreadable CSV"))
    );
}

// =============================================================================================
// persistence: PRI and TRI must coexist
// =============================================================================================

#[tokio::test]
async fn pri_and_tri_occupy_separate_columns() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(
        &mut c,
        "NIFTY 500",
        &pri(&[("2026-01-01", 20_000.0), ("2026-01-02", 20_100.0)]),
        PRI,
    )
    .await
    .unwrap();
    b::upsert_benchmark(
        &mut c,
        "NIFTY 500",
        &tri(&[("2026-01-01", 30_000.0), ("2026-01-02", 30_200.0)]),
        TRI,
    )
    .await
    .unwrap();
    let rows = c
        .execute("SELECT date, close, tri FROM benchmark ORDER BY date", &[])
        .await
        .unwrap()
        .rows;
    assert_eq!(
        rows.iter().map(|r| f(r.get("close"))).collect::<Vec<_>>(),
        [Some(20_000.0), Some(20_100.0)]
    );
    assert_eq!(
        rows.iter().map(|r| f(r.get("tri"))).collect::<Vec<_>>(),
        [Some(30_000.0), Some(30_200.0)]
    );
}

#[tokio::test]
async fn loading_tri_does_not_clobber_pri_in_either_order() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(&mut c, "X", &tri(&[("2026-01-01", 30_000.0)]), TRI)
        .await
        .unwrap();
    b::upsert_benchmark(&mut c, "X", &pri(&[("2026-01-01", 20_000.0)]), PRI)
        .await
        .unwrap();
    let rows = c
        .execute("SELECT close, tri FROM benchmark WHERE index_name='X'", &[])
        .await
        .unwrap()
        .rows;
    assert_eq!(f(rows[0].get("close")), Some(20_000.0));
    assert_eq!(f(rows[0].get("tri")), Some(30_000.0));
}

#[tokio::test]
async fn reingesting_updates_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(&mut c, "X", &pri(&[("2026-01-01", 20_000.0)]), PRI)
        .await
        .unwrap();
    b::upsert_benchmark(&mut c, "X", &pri(&[("2026-01-01", 20_500.0)]), PRI)
        .await
        .unwrap();
    let rows = c
        .execute("SELECT close FROM benchmark WHERE index_name='X'", &[])
        .await
        .unwrap()
        .rows;
    assert_eq!(rows.len(), 1);
    assert_eq!(f(rows[0].get("close")), Some(20_500.0));
}

#[tokio::test]
async fn upsert_rejects_an_unknown_kind() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    let e = b::upsert_benchmark(&mut c, "X", &[], "TOTAL")
        .await
        .unwrap_err();
    assert_eq!(e.to_string(), "kind must be PRI or TRI, got 'TOTAL'");
}

#[tokio::test]
async fn upsert_accepts_plain_pairs_and_reports_the_span() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    let res = b::upsert_benchmark(
        &mut c,
        "X",
        &tri(&[("2026-01-01", 100.0), ("2026-01-02", 101.0)]),
        TRI,
    )
    .await
    .unwrap();
    assert_eq!(
        (res.rows, res.first.as_deref(), res.last.as_deref()),
        (2, Some("2026-01-01"), Some("2026-01-02"))
    );
    let n = c
        .execute("SELECT COUNT(*) c FROM benchmark", &[])
        .await
        .unwrap()
        .rows;
    assert_eq!(i(n[0].get("c")), 2);
}

#[test]
fn a_series_with_gaps_writes_only_present_values() {
    let pairs = b::series_pairs(&[(d("2026-01-01"), 1.0), (d("2026-01-02"), f64::NAN)]);
    assert_eq!(pairs, [("2026-01-01".to_owned(), 1.0)]);
}

// =============================================================================================
// reading back: the series MUST say which one it is
// =============================================================================================

#[tokio::test]
async fn series_prefers_tri_and_labels_it() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(
        &mut c,
        "N500",
        &pri(&[("2026-01-01", 20_000.0), ("2026-01-02", 20_100.0)]),
        PRI,
    )
    .await
    .unwrap();
    b::upsert_benchmark(
        &mut c,
        "N500",
        &tri(&[("2026-01-01", 30_000.0), ("2026-01-02", 30_300.0)]),
        TRI,
    )
    .await
    .unwrap();
    let s = b::benchmark_series(&mut c, "N500", TRI).await.unwrap();
    assert_eq!(s.series_type, Some(TRI));
    assert_eq!(s.series[0].1, 30_000.0);
    assert!(s.series[1].1 / s.series[0].1 > 20_100.0 / 20_000.0);
}

#[tokio::test]
async fn series_falls_back_to_pri_when_no_tri_exists() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(&mut c, "N500", &pri(&[("2026-01-01", 20_000.0)]), PRI)
        .await
        .unwrap();
    let s = b::benchmark_series(&mut c, "N500", TRI).await.unwrap();
    assert_eq!(s.series_type, Some(PRI));
    assert_eq!(s.series[0].1, 20_000.0);
}

#[tokio::test]
async fn series_can_be_forced_to_pri() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(&mut c, "N500", &pri(&[("2026-01-01", 20_000.0)]), PRI)
        .await
        .unwrap();
    b::upsert_benchmark(&mut c, "N500", &tri(&[("2026-01-01", 30_000.0)]), TRI)
        .await
        .unwrap();
    assert_eq!(
        b::benchmark_series(&mut c, "N500", PRI)
            .await
            .unwrap()
            .series[0]
            .1,
        20_000.0
    );
}

#[tokio::test]
async fn unknown_index_returns_an_empty_series() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    let s = b::benchmark_series(&mut c, "NOPE", TRI).await.unwrap();
    assert!(s.series.is_empty());
    assert_eq!(s.series_type, None);
}

#[tokio::test]
async fn available_benchmarks_summarises_coverage() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = conn(dir.path()).await;
    b::upsert_benchmark(
        &mut c,
        "A",
        &pri(&[("2026-01-01", 1.0), ("2026-01-02", 2.0)]),
        PRI,
    )
    .await
    .unwrap();
    b::upsert_benchmark(&mut c, "B", &tri(&[("2026-01-01", 5.0)]), TRI)
        .await
        .unwrap();
    let rows = b::available_benchmarks(&mut c).await.unwrap();
    let a = &rows[0];
    let bb = &rows[1];
    assert_eq!((i(a.get("pri")), i(a.get("tri"))), (2, 0));
    assert_eq!((i(bb.get("pri")), i(bb.get("tri"))), (0, 1));
    assert_eq!(a.get("first"), Some(&DbValue::Text("2026-01-01".into())));
    assert_eq!(a.get("last"), Some(&DbValue::Text("2026-01-02".into())));
}

#[test]
fn common_index_aliases_match_the_live_dump_strings() {
    assert_eq!(b::common_index("nifty500"), Some("NIFTY 500"));
    assert_eq!(b::common_index("momentum30"), Some("NIFTY200MOMENTM30"));
    assert!(!b::common_index("momentum30").unwrap().contains(' '));
}

// =============================================================================================
// PRI resolution over a recorded dump (reads only)
// =============================================================================================

struct Dump;

#[async_trait::async_trait]
impl b::IndexKite for Dump {
    async fn is_authed(&self) -> bool {
        true
    }
    async fn instruments(&self) -> Result<Vec<baskfy_desk::wire::PyValue>, String> {
        let raw = br#"[
            {"tradingsymbol": "NIFTY 500", "instrument_token": 268041, "segment": "INDICES", "exchange": "NSE", "name": "NIFTY 500"},
            {"tradingsymbol": "NIFTY200MOMENTM30", "instrument_token": 260617, "segment": "INDICES", "exchange": "NSE", "name": ""},
            {"tradingsymbol": "RELIANCE", "instrument_token": 738561, "segment": "NSE", "exchange": "NSE", "name": "RELIANCE"}
        ]"#;
        match baskfy_desk::wire::PyValue::from_json(raw).unwrap() {
            baskfy_desk::wire::PyValue::List(v) => Ok(v),
            _ => unreachable!(),
        }
    }
    async fn historical(
        &self,
        _t: i64,
        _a: NaiveDate,
        _b: NaiveDate,
    ) -> Result<Vec<baskfy_desk::wire::PyValue>, String> {
        let raw = br#"[{"date": "2026-01-01T00:00:00+05:30", "close": 20000.5}, {"date": "2026-01-02T00:00:00+05:30", "close": 20100}]"#;
        match baskfy_desk::wire::PyValue::from_json(raw).unwrap() {
            baskfy_desk::wire::PyValue::List(v) => Ok(v),
            _ => unreachable!(),
        }
    }
}

#[tokio::test]
async fn index_names_resolve_exactly_by_alias_and_never_to_an_equity() {
    let dir = tempfile::tempdir().unwrap();
    let today = d("2026-08-14");
    let r = b::resolve_index_token(&Dump, dir.path(), today, "momentum30", &[])
        .await
        .unwrap();
    assert_eq!(r.instrument_token, 260_617);
    let r = b::resolve_index_token(&Dump, dir.path(), today, "nifty 500", &[])
        .await
        .unwrap();
    assert_eq!(r.tradingsymbol, "NIFTY 500");
    let e = b::resolve_index_token(&Dump, dir.path(), today, "RELIANCE", &[])
        .await
        .unwrap_err();
    assert!(matches!(e, BenchError::Lookup(m) if m.starts_with("index 'RELIANCE' not found")));
    let listed = b::list_indices(&Dump, dir.path(), today, "nifty")
        .await
        .unwrap();
    assert_eq!(listed.len(), 2);
    assert!(dir.path().join(b::INSTRUMENT_CACHE).exists());
}

#[tokio::test]
async fn kite_series_is_labelled_price_return() {
    let dir = tempfile::tempdir().unwrap();
    let s = b::kite_index_series(
        &Dump,
        dir.path(),
        d("2026-01-02"),
        "NIFTY 500",
        &[],
        d("2026-01-01"),
        None,
    )
    .await
    .unwrap();
    assert_eq!(s.series_type, Some(PRI));
    assert_eq!(
        s.series,
        [(d("2026-01-01"), 20_000.5), (d("2026-01-02"), 20_100.0)]
    );
}
