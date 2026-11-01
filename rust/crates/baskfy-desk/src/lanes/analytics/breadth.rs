//! `app/analytics/breadth.py`: the breadth adapter — provenance for the one number
//! `scoring.audit()` reports (`breadth_above_20dma`), and its storage in `breadth_readings`.
//!
//! The denominator is `scoring.audit`'s: every row, a NaN close or NaN `ma_20` counted as
//! **below** (`missing_policy="counted_as_below"`); `coverage_pct` says how much of the universe
//! had usable data. A different eligible set is a different `universe_hash`, so readings from
//! two universe definitions are never silently compared ([`comparable`]).
//!
//! `reading_from_audit` takes the scan as plain columns ([`ScanColumns`]) rather than a
//! DataFrame; the arithmetic is the same.

use chrono::NaiveDate;
use sha2::Digest as _;

use crate::storage::{Conn, DbValue, Param, Row, StorageError};

/// `CALCULATION_VERSION`.
pub const CALCULATION_VERSION: &str = "breadth/1.0.0";
/// `MISSING_POLICY`.
pub const MISSING_POLICY: &str = "counted_as_below";

/// `core.regime.BreadthReading`.
#[derive(Debug, Clone, PartialEq)]
pub struct BreadthReading {
    /// `as_of_date`.
    pub as_of_date: NaiveDate,
    /// `pct_above_20dma`.
    pub pct_above_20dma: f64,
    /// `eligible_count`.
    pub eligible_count: i64,
    /// `observed_count`.
    pub observed_count: i64,
    /// `coverage_pct`.
    pub coverage_pct: f64,
    /// `universe_id`.
    pub universe_id: String,
    /// `universe_hash`.
    pub universe_hash: String,
    /// `audit_run_id`.
    pub audit_run_id: String,
    /// `calculation_version`.
    pub calculation_version: String,
}

fn sha16(text: &str) -> String {
    let digest = sha2::Sha256::digest(text.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    hex[..16].to_owned()
}

/// `universe_hash(symbols)`: stable over content, not order.
#[must_use]
pub fn universe_hash<S: AsRef<str>>(symbols: &[S]) -> String {
    let mut v: Vec<&str> = symbols.iter().map(AsRef::as_ref).collect();
    v.sort_unstable();
    sha16(&v.join("|"))
}

/// `audit_run_id(universe_id, as_of, uhash)`.
#[must_use]
pub fn audit_run_id(universe_id: &str, as_of: NaiveDate, uhash: &str) -> String {
    sha16(&format!(
        "{universe_id}|{}|{uhash}|{CALCULATION_VERSION}",
        as_of.format("%Y-%m-%d")
    ))
}

/// The scan frame's columns `reading_from_audit` reads.
#[derive(Debug, Clone, Default)]
pub struct ScanColumns {
    /// `scan["symbol"]`, if the column exists.
    pub symbol: Option<Vec<String>>,
    /// `scan["close"]` and `scan["ma_20"]` (NaN as `None`), if both columns exist.
    pub close_ma20: Option<Vec<(Option<f64>, Option<f64>)>>,
    /// `len(scan)`.
    pub rows: usize,
}

/// `reading_from_audit(audit, scan, universe_id=..., as_of=...)`. `as_of` is required here:
/// Python's fallbacks (the audit's newest `scan_date`, else today) are the caller's.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn reading_from_audit(
    breadth_above_20dma: f64,
    scan: &ScanColumns,
    universe_id: &str,
    as_of: NaiveDate,
) -> BreadthReading {
    let eligible = scan.rows;
    let observed = scan.close_ma20.as_ref().map_or(eligible, |cols| {
        cols.iter()
            .filter(|(c, m)| c.is_some_and(|x| !x.is_nan()) && m.is_some_and(|x| !x.is_nan()))
            .count()
    });
    let coverage = if eligible == 0 {
        0.0
    } else {
        observed as f64 / eligible as f64 * 100.0
    };
    let empty: Vec<String> = Vec::new();
    let uhash = universe_hash(scan.symbol.as_ref().unwrap_or(&empty));
    BreadthReading {
        as_of_date: as_of,
        pct_above_20dma: breadth_above_20dma,
        eligible_count: i64::try_from(eligible).unwrap_or(i64::MAX),
        observed_count: i64::try_from(observed).unwrap_or(i64::MAX),
        coverage_pct: crate::lanes::weekly::pandas::py_round(coverage, 4),
        universe_id: universe_id.to_owned(),
        audit_run_id: audit_run_id(universe_id, as_of, &uhash),
        universe_hash: uhash,
        calculation_version: CALCULATION_VERSION.to_owned(),
    }
}

/// `save_reading(conn, reading)`: idempotent upsert keyed `(as_of_date, universe_id)`;
/// `"inserted"` or `"updated"`.
///
/// # Errors
/// The driver's.
pub async fn save_reading(
    conn: &mut Conn,
    reading: &BreadthReading,
    missing_policy: &str,
    now_iso: &str,
) -> Result<&'static str, StorageError> {
    let day = reading.as_of_date.format("%Y-%m-%d").to_string();
    let existing = conn
        .execute(
            "SELECT 1 FROM breadth_readings WHERE as_of_date=? AND universe_id=?",
            &[day.as_str().into(), reading.universe_id.as_str().into()],
        )
        .await?
        .fetchone()
        .is_some();
    let args: Vec<Param> = vec![
        day.into(),
        reading.universe_id.clone().into(),
        reading.pct_above_20dma.into(),
        reading.eligible_count.into(),
        reading.observed_count.into(),
        reading.coverage_pct.into(),
        reading.universe_hash.clone().into(),
        reading.audit_run_id.clone().into(),
        reading.calculation_version.clone().into(),
        missing_policy.into(),
        now_iso.into(),
    ];
    conn.transaction(move |c| {
        Box::pin(async move {
            c.execute(
                "INSERT INTO breadth_readings(as_of_date, universe_id, pct_above_20dma, \
                 eligible_count, observed_count, coverage_pct, universe_hash, audit_run_id, \
                 calculation_version, missing_policy, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(as_of_date, universe_id) DO UPDATE SET \
                 pct_above_20dma=excluded.pct_above_20dma, eligible_count=excluded.eligible_count, \
                 observed_count=excluded.observed_count, coverage_pct=excluded.coverage_pct, \
                 universe_hash=excluded.universe_hash, audit_run_id=excluded.audit_run_id, \
                 calculation_version=excluded.calculation_version, \
                 missing_policy=excluded.missing_policy",
                &args,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(if existing { "updated" } else { "inserted" })
}

fn text(r: &Row, k: &str) -> String {
    match r.get(k) {
        Some(DbValue::Text(s) | DbValue::Json(s)) => s.clone(),
        Some(DbValue::Int(i)) => i.to_string(),
        _ => String::new(),
    }
}

fn num(r: &Row, k: &str) -> f64 {
    r.get(k).and_then(DbValue::as_f64).unwrap_or(f64::NAN)
}

fn int(r: &Row, k: &str) -> i64 {
    r.get(k).and_then(DbValue::as_i64).unwrap_or(0)
}

fn row_to_reading(r: &Row) -> Option<BreadthReading> {
    let as_of = super::ops::parse_iso_date(&text(r, "as_of_date"))?;
    Some(BreadthReading {
        as_of_date: as_of,
        pct_above_20dma: num(r, "pct_above_20dma"),
        eligible_count: int(r, "eligible_count"),
        observed_count: int(r, "observed_count"),
        coverage_pct: num(r, "coverage_pct"),
        universe_id: text(r, "universe_id"),
        universe_hash: text(r, "universe_hash"),
        audit_run_id: text(r, "audit_run_id"),
        calculation_version: text(r, "calculation_version"),
    })
}

/// `latest_reading(conn, universe_id=..., on_or_before=...)`.
///
/// # Errors
/// The driver's (or a stored date Python could not parse, as an unhandled error).
pub async fn latest_reading(
    conn: &mut Conn,
    universe_id: &str,
    on_or_before: Option<NaiveDate>,
) -> Result<Option<BreadthReading>, StorageError> {
    let mut sql = "SELECT * FROM breadth_readings WHERE universe_id=?".to_owned();
    let mut args: Vec<Param> = vec![universe_id.into()];
    if let Some(d) = on_or_before {
        sql.push_str(" AND as_of_date <= ?");
        args.push(d.format("%Y-%m-%d").to_string().into());
    }
    sql.push_str(" ORDER BY as_of_date DESC LIMIT 1");
    let cur = conn.execute(&sql, &args).await?;
    match cur.fetchone() {
        None => Ok(None),
        Some(r) => row_to_reading(r)
            .map(Some)
            .ok_or_else(|| StorageError::Other("ValueError: Invalid isoformat string".to_owned())),
    }
}

/// `history(conn, universe_id=...)`.
///
/// # Errors
/// The driver's.
pub async fn history(
    conn: &mut Conn,
    universe_id: &str,
) -> Result<Vec<BreadthReading>, StorageError> {
    let cur = conn
        .execute(
            "SELECT * FROM breadth_readings WHERE universe_id=? ORDER BY as_of_date",
            &[universe_id.into()],
        )
        .await?;
    // Python's list comprehension raises on the first row it cannot read; it never skips one.
    cur.rows
        .iter()
        .map(|r| {
            row_to_reading(r).ok_or_else(|| {
                StorageError::Other("ValueError: Invalid isoformat string".to_owned())
            })
        })
        .collect()
}

/// `comparable(a, b)`: false across universe definitions or versions.
#[must_use]
pub fn comparable(a: &BreadthReading, b: &BreadthReading) -> bool {
    a.universe_hash == b.universe_hash
        && a.calculation_version == b.calculation_version
        && a.universe_id == b.universe_id
}

/// `reading_from_audit(audit, scan, universe_id=, as_of=None)` over `scoring.audit()`'s dict,
/// with Python's fallbacks: the audit's newest `scan_date`, else `today`. The audit's breadth
/// value is consumed as-is.
///
/// # Errors
/// `ValueError` when the dict carries no `breadth_above_20dma` (wrong source), or a value Python
/// cannot read.
pub fn reading_from_audit_dict(
    audit: &crate::wire::PyDict,
    scan: &ScanColumns,
    universe_id: &str,
    as_of: Option<NaiveDate>,
    today: NaiveDate,
) -> Result<BreadthReading, String> {
    use crate::wire::PyValue;
    let Some(value) = audit.get("breadth_above_20dma") else {
        return Err(
            "ValueError: audit dict has no 'breadth_above_20dma' — wrong source?".to_owned(),
        );
    };
    let pct = match value {
        PyValue::Float(f) => *f,
        #[allow(clippy::cast_precision_loss)]
        PyValue::Int(i) => *i as f64,
        PyValue::Str(s) => crate::settings::py_float(s)
            .ok_or_else(|| format!("ValueError: could not convert string to float: '{s}'"))?,
        other => {
            return Err(format!(
                "TypeError: float() argument: {}",
                other.type_name()
            ));
        }
    };
    let as_of = match as_of {
        Some(d) => d,
        None => {
            let mut dates: Vec<String> = match audit.get("scan_date") {
                Some(PyValue::List(items)) => items.iter().map(PyValue::py_str).collect(),
                _ => Vec::new(),
            };
            dates.sort();
            match dates.last() {
                Some(last) => {
                    let head: String = last.chars().take(10).collect();
                    super::ops::parse_iso_date(&head)
                        .ok_or_else(|| format!("ValueError: Invalid isoformat string: '{head}'"))?
                }
                None => today,
            }
        }
    };
    Ok(reading_from_audit(pct, scan, universe_id, as_of))
}

/// `distinct_universes(conn, universe_id=)`: every universe definition the stored history spans.
///
/// # Errors
/// The driver's.
pub async fn distinct_universes(
    conn: &mut Conn,
    universe_id: &str,
) -> Result<Vec<crate::wire::PyDict>, StorageError> {
    let cur = conn
        .execute(
            "SELECT universe_hash, calculation_version, COUNT(*) n, \
             MIN(as_of_date) first, MAX(as_of_date) last \
             FROM breadth_readings WHERE universe_id=? \
             GROUP BY universe_hash, calculation_version ORDER BY first",
            &[universe_id.into()],
        )
        .await?;
    Ok(cur.rows.iter().map(super::web::row_dict).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn day(s: &str) -> NaiveDate {
        NaiveDate::parse_from_str(s, "%Y-%m-%d").unwrap_or_default()
    }

    #[test]
    fn universe_hash_ignores_order_and_sees_content() {
        assert_eq!(
            universe_hash(&["B", "A", "C"]),
            universe_hash(&["C", "B", "A"])
        );
        assert_ne!(universe_hash(&["A", "B"]), universe_hash(&["A", "B", "C"]));
        // hashlib.sha256(b"A|B|C").hexdigest()[:16]
        assert_eq!(universe_hash(&["C", "A", "B"]), "8c3d170c3a6a5100");
    }

    #[test]
    fn coverage_counts_missing_as_unobserved_but_keeps_them_eligible() {
        let scan = ScanColumns {
            symbol: Some(vec!["A".into(), "B".into(), "C".into(), "D".into()]),
            close_ma20: Some(vec![
                (Some(10.0), Some(9.0)),
                (Some(10.0), None),
                (Some(f64::NAN), Some(9.0)),
                (Some(8.0), Some(9.0)),
            ]),
            rows: 4,
        };
        let r = reading_from_audit(25.0, &scan, "scan", day("2026-10-30"));
        assert_eq!(r.eligible_count, 4);
        assert_eq!(r.observed_count, 2);
        assert!((r.coverage_pct - 50.0).abs() < 1e-12);
        // the audit's breadth is authoritative and untouched
        assert!((r.pct_above_20dma - 25.0).abs() < 1e-12);
        assert_eq!(r.calculation_version, CALCULATION_VERSION);
        assert_eq!(
            r.audit_run_id,
            audit_run_id("scan", day("2026-10-30"), &r.universe_hash)
        );
    }

    #[test]
    fn readings_from_different_universes_are_not_comparable() {
        let scan = |syms: &[&str]| ScanColumns {
            symbol: Some(syms.iter().map(|s| (*s).to_owned()).collect()),
            close_ma20: None,
            rows: syms.len(),
        };
        let a = reading_from_audit(50.0, &scan(&["A", "B"]), "scan", day("2026-10-23"));
        let b = reading_from_audit(60.0, &scan(&["B", "A"]), "scan", day("2026-10-30"));
        let c = reading_from_audit(60.0, &scan(&["A", "B", "C"]), "scan", day("2026-10-30"));
        assert!(comparable(&a, &b));
        assert!(!comparable(&a, &c));
        assert_eq!(a.coverage_pct, 100.0);
    }

    #[tokio::test]
    async fn save_is_an_idempotent_upsert() {
        let dir = tempfile::tempdir().map_err(|e| e.to_string());
        let Ok(dir) = dir else { return };
        let Ok(mut conn) = crate::storage::Db::connect_sqlite_path(&dir.path().join("t.db")).await
        else {
            return;
        };
        assert!(conn.migrate().await.is_ok());
        let scan = ScanColumns {
            symbol: Some(vec!["A".into()]),
            close_ma20: None,
            rows: 1,
        };
        let r = reading_from_audit(40.0, &scan, "scan", day("2026-10-30"));
        assert_eq!(
            save_reading(&mut conn, &r, MISSING_POLICY, "2026-10-30T16:00:00")
                .await
                .ok(),
            Some("inserted")
        );
        assert_eq!(
            save_reading(&mut conn, &r, MISSING_POLICY, "2026-10-30T16:00:01")
                .await
                .ok(),
            Some("updated")
        );
        let got = latest_reading(&mut conn, "scan", None).await.ok().flatten();
        assert_eq!(got.as_ref(), Some(&r));
        assert_eq!(
            history(&mut conn, "scan").await.map(|h| h.len()).ok(),
            Some(1)
        );
        assert!(
            latest_reading(&mut conn, "scan", Some(day("2026-10-29")))
                .await
                .ok()
                .flatten()
                .is_none()
        );
    }
}
