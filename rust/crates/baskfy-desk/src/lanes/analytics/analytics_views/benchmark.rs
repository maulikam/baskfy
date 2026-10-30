//! `app/analytics/benchmark.py`: PRI from Kite (reads only), TRI from niftyindices.com CSVs, and
//! the `benchmark` table both fill without clobbering each other.

use std::path::Path;

use chrono::NaiveDate;

use super::metrics::Series;
use crate::storage::{Conn, DbValue, Param, Row, StorageError};
use crate::wire::PyValue;

/// Index-segment rows only.
pub const INSTRUMENT_CACHE: &str = "data/.instruments_indices.json";
/// `INDEX_SEGMENTS`.
pub const INDEX_SEGMENTS: [&str; 2] = ["INDICES", "NSE-INDICES"];
/// Price return.
pub const PRI: &str = "PRI";
/// Total return.
pub const TRI: &str = "TRI";

/// `COMMON_INDICES`: aliases → the tradingsymbol in the dump (`NIFTY200MOMENTM30` has no spaces).
pub const COMMON_INDICES: [(&str, &str); 6] = [
    ("nifty500", "NIFTY 500"),
    ("nifty50", "NIFTY 50"),
    ("momentum30", "NIFTY200MOMENTM30"),
    ("momentum50", "NIFTY500MOMENTM50"),
    ("midcap150", "NIFTY MIDCAP 150"),
    ("smallcap250", "NIFTY SMLCAP 250"),
];

/// `COMMON_INDICES[alias]`.
#[must_use]
pub fn common_index(alias: &str) -> Option<&'static str> {
    COMMON_INDICES
        .iter()
        .find(|(k, _)| *k == alias)
        .map(|(_, v)| *v)
}

/// A Python exception escaping the module.
#[derive(Debug, thiserror::Error)]
pub enum BenchError {
    /// `ValueError`.
    #[error("{0}")]
    Value(String),
    /// `LookupError`.
    #[error("{0}")]
    Lookup(String),
    /// `RuntimeError`.
    #[error("{0}")]
    Runtime(String),
    /// A Kite read.
    #[error("{0}")]
    Kite(String),
    /// The database.
    #[error(transparent)]
    Storage(#[from] StorageError),
}

// =============================================================================================
// instrument resolution and PRI (reads only)
// =============================================================================================

/// The Kite reads the PRI path makes (`instruments()`, `historical(token, from, to, "day")`).
#[async_trait::async_trait]
pub trait IndexKite: Send + Sync {
    /// `is_authed()`.
    async fn is_authed(&self) -> bool;
    /// `instruments()` (the NSE dump).
    async fn instruments(&self) -> Result<Vec<PyValue>, String>;
    /// `historical(token, start, end, "day")`.
    async fn historical(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<Vec<PyValue>, String>;
}

#[async_trait::async_trait]
impl IndexKite for crate::lanes::exec_core::kite_client::DeskKite {
    async fn is_authed(&self) -> bool {
        Self::is_authed(self).await
    }
    async fn instruments(&self) -> Result<Vec<PyValue>, String> {
        Self::instruments(self, "NSE")
            .await
            .map_err(|e| e.to_string())
    }
    async fn historical(
        &self,
        token: i64,
        start: NaiveDate,
        end: NaiveDate,
    ) -> Result<Vec<PyValue>, String> {
        let (a, b) = (
            start.format("%Y-%m-%d").to_string(),
            end.format("%Y-%m-%d").to_string(),
        );
        Self::historical(self, token, &a, &b, "day")
            .await
            .map_err(|e| e.to_string())
    }
}

/// One index instrument from the dump.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexInstrument {
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `instrument_token`.
    pub instrument_token: i64,
    /// `segment`.
    pub segment: String,
    /// `exchange`.
    pub exchange: String,
    /// `name`.
    pub name: String,
}

fn text(v: Option<&PyValue>) -> String {
    match v {
        None | Some(PyValue::None) => String::new(),
        Some(PyValue::Str(s)) => s.clone(),
        Some(other) => other.py_str(),
    }
}

impl IndexInstrument {
    fn from_py(v: &PyValue) -> Option<Self> {
        let PyValue::Map(m) = v else { return None };
        let token = match m.get("instrument_token") {
            Some(PyValue::Int(i)) => *i,
            #[allow(clippy::cast_possible_truncation)]
            Some(PyValue::Float(f)) => *f as i64,
            Some(PyValue::Str(s)) => s.trim().parse().ok()?,
            _ => return None,
        };
        Some(Self {
            tradingsymbol: text(m.get("tradingsymbol")),
            instrument_token: token,
            segment: text(m.get("segment")),
            exchange: text(m.get("exchange")),
            name: text(m.get("name")),
        })
    }

    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "tradingsymbol": self.tradingsymbol,
            "instrument_token": self.instrument_token,
            "segment": self.segment,
            "exchange": self.exchange,
            "name": self.name,
        })
    }
}

fn is_index_segment(seg: &str) -> bool {
    INDEX_SEGMENTS.contains(&seg.to_uppercase().as_str())
}

fn cached_rows(path: &Path, day: &str) -> Option<Vec<IndexInstrument>> {
    let bytes = std::fs::read(path).ok()?;
    let blob = serde_json::from_slice::<serde_json::Value>(&bytes).ok()?;
    if blob.get("fetched").and_then(serde_json::Value::as_str) != Some(day) {
        return None;
    }
    let rows = blob.get("rows").and_then(serde_json::Value::as_array)?;
    let raw = serde_json::to_vec(rows).ok()?;
    let PyValue::List(items) = PyValue::from_json(&raw).ok()? else {
        return None;
    };
    Some(
        items
            .iter()
            .filter_map(IndexInstrument::from_py)
            .filter(|r| is_index_segment(&r.segment))
            .collect(),
    )
}

/// `_index_dump(kite, refresh=)`: index rows from the dump, cached to `root/INSTRUMENT_CACHE`
/// for the day (filtered on read as well as on write).
///
/// # Errors
/// The Kite read.
pub async fn index_dump(
    kite: &dyn IndexKite,
    root: &Path,
    today: NaiveDate,
    refresh: bool,
) -> Result<Vec<IndexInstrument>, BenchError> {
    let path = root.join(INSTRUMENT_CACHE);
    let day = today.format("%Y-%m-%d").to_string();
    if !refresh && let Some(rows) = cached_rows(&path, &day) {
        return Ok(rows);
    }
    let dump = kite.instruments().await.map_err(BenchError::Kite)?;
    let rows: Vec<IndexInstrument> = dump
        .iter()
        .filter_map(IndexInstrument::from_py)
        .filter(|r| INDEX_SEGMENTS.contains(&r.segment.as_str()))
        .collect();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let blob = serde_json::json!({
        "fetched": day,
        "rows": rows.iter().map(IndexInstrument::to_json).collect::<Vec<_>>(),
    });
    let _ = std::fs::write(&path, blob.to_string());
    Ok(rows)
}

/// `list_indices(contains=)`: every index instrument, optionally filtered, by tradingsymbol.
///
/// # Errors
/// The Kite read.
pub async fn list_indices(
    kite: &dyn IndexKite,
    root: &Path,
    today: NaiveDate,
    contains: &str,
) -> Result<Vec<IndexInstrument>, BenchError> {
    let mut rows = index_dump(kite, root, today, false).await?;
    if !contains.is_empty() {
        let needle = contains.to_uppercase();
        rows.retain(|r| r.tradingsymbol.to_uppercase().contains(&needle));
    }
    rows.sort_by(|a, b| a.tradingsymbol.cmp(&b.tradingsymbol));
    Ok(rows)
}

/// `resolve_index_token(index_name)`: exact, then spacing-tolerant, then a unique partial match.
/// `aliases` are the regime config's `index_aliases[index_name]`.
///
/// # Errors
/// `LookupError` naming candidates; the Kite read.
pub async fn resolve_index_token(
    kite: &dyn IndexKite,
    root: &Path,
    today: NaiveDate,
    index_name: &str,
    aliases: &[String],
) -> Result<IndexInstrument, BenchError> {
    let rows = index_dump(kite, root, today, false).await?;
    let mut accepted: Vec<String> = vec![index_name.trim().to_uppercase()];
    if let Some(m) = common_index(&index_name.to_lowercase().replace(' ', "")) {
        accepted.push(m.to_uppercase());
    }
    for a in aliases {
        accepted.push(a.trim().to_uppercase());
    }
    if let Some(r) = rows
        .iter()
        .find(|r| accepted.contains(&r.tradingsymbol.to_uppercase()))
    {
        return Ok(r.clone());
    }
    let mut squashed: Vec<String> = accepted.iter().map(|a| a.replace(' ', "")).collect();
    squashed.sort();
    squashed.dedup();
    if let Some(r) = rows
        .iter()
        .find(|r| squashed.contains(&r.tradingsymbol.to_uppercase().replace(' ', "")))
    {
        return Ok(r.clone());
    }
    let first = squashed.first().cloned().unwrap_or_default();
    let rows: Vec<&IndexInstrument> = rows
        .iter()
        .filter(|r| is_index_segment(&r.segment))
        .collect();
    let partial: Vec<&IndexInstrument> = rows
        .iter()
        .copied()
        .filter(|r| {
            r.tradingsymbol
                .to_uppercase()
                .replace(' ', "")
                .contains(&first)
        })
        .collect();
    if partial.len() == 1 {
        return Ok(partial[0].clone());
    }
    let names = |v: &[&IndexInstrument]| {
        let mut n: Vec<String> = v.iter().map(|r| r.tradingsymbol.clone()).collect();
        n.sort();
        n.truncate(10);
        n.join(", ")
    };
    let hint = if partial.is_empty() {
        names(&rows)
    } else {
        names(&partial)
    };
    Err(BenchError::Lookup(format!(
        "index {} not found in the live dump. Candidates: {hint}",
        PyValue::Str(index_name.to_owned()).py_repr()
    )))
}

/// A stored or fetched benchmark series, labelled with what it is.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct BenchSeries {
    /// The levels by date.
    pub series: Series,
    /// `PRI` or `TRI` (`None` for an empty series).
    pub series_type: Option<&'static str>,
    /// `index_name`.
    pub index_name: String,
}

fn candle_date(v: Option<&PyValue>) -> Option<NaiveDate> {
    match v? {
        PyValue::Date(d) => Some(*d),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Naive(t)) => Some(t.date()),
        PyValue::DateTime(baskfy_providers::records::PyDateTime::Aware(t)) => Some(t.date_naive()),
        PyValue::Str(s) => NaiveDate::parse_from_str(s.get(..10)?, "%Y-%m-%d").ok(),
        _ => None,
    }
}

/// `kite_index_series(index_name, start, end)`: daily PRICE-RETURN closes, in 1800-day chunks.
///
/// # Errors
/// `RuntimeError` without a session; resolution; the Kite read.
pub async fn kite_index_series(
    kite: &dyn IndexKite,
    root: &Path,
    today: NaiveDate,
    index_name: &str,
    aliases: &[String],
    start: NaiveDate,
    end: Option<NaiveDate>,
) -> Result<BenchSeries, BenchError> {
    if !kite.is_authed().await {
        return Err(BenchError::Runtime(
            "Kite session expired — log in at / first (token is daily).".to_owned(),
        ));
    }
    let inst = resolve_index_token(kite, root, today, index_name, aliases).await?;
    let to = end.unwrap_or(today);
    let mut out: std::collections::BTreeMap<NaiveDate, f64> = std::collections::BTreeMap::new();
    let mut cursor = start;
    while cursor <= to {
        let chunk_end = (cursor + chrono::Duration::days(1800)).min(to);
        for c in kite
            .historical(inst.instrument_token, cursor, chunk_end)
            .await
            .map_err(BenchError::Kite)?
        {
            let PyValue::Map(m) = &c else { continue };
            let (Some(d), Some(close)) = (
                candle_date(m.get("date")),
                m.get("close")
                    .map(|v| crate::lanes::weekly::weekly_books::pyf(Some(v))),
            ) else {
                continue;
            };
            out.insert(d, close);
        }
        cursor = chunk_end + chrono::Duration::days(1);
    }
    Ok(BenchSeries {
        series: out.into_iter().collect(),
        series_type: Some(PRI),
        index_name: inst.tradingsymbol,
    })
}

// =============================================================================================
// TRI — niftyindices.com CSV
// =============================================================================================

const TRI_DATE_FORMATS: [&str; 5] = ["%d %b %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %B %Y"];

/// `_parse_tri_date(raw)`: the export's formats, then day-first fallbacks.
#[must_use]
pub fn parse_tri_date(raw: &str) -> Option<String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    TRI_DATE_FORMATS
        .iter()
        .chain(
            [
                "%d-%m-%Y",
                "%d.%m.%Y",
                "%Y/%m/%d",
                "%d %b, %Y",
                "%b %d, %Y",
                "%B %d, %Y",
            ]
            .iter(),
        )
        .find_map(|f| NaiveDate::parse_from_str(raw, f).ok())
        .map(|d| d.format("%Y-%m-%d").to_string())
}

/// One TRI row.
#[derive(Debug, Clone, PartialEq)]
pub struct TriRow {
    /// ISO date.
    pub date: String,
    /// Gross TRI (or net when no gross column).
    pub tri: f64,
    /// Net TRI, when exported.
    pub net_tri: Option<f64>,
}

/// `tri_csv_loader(path)`: lenient columns and dates; refuses a row wider than the header.
///
/// # Errors
/// `ValueError` (unreadable, missing columns, an unquoted thousands separator, no rows).
pub fn tri_csv_loader(path: &Path) -> Result<Vec<TriRow>, BenchError> {
    let shown = path.display().to_string();
    let raw = std::fs::read(path).map_err(|e| BenchError::Value(format!("{shown}: {e}")))?;
    let text = String::from_utf8_lossy(&raw);
    let rows = crate::lanes::analytics::analytics_regime::nse_indices::csv_rows(
        text.trim_start_matches('\u{feff}'),
    );
    let mut it = rows.into_iter();
    let header = match it.next() {
        Some(h) if !h.is_empty() => h,
        _ => {
            return Err(BenchError::Value(format!(
                "{shown}: empty or unreadable CSV"
            )));
        }
    };
    let mut cols: Vec<(String, usize)> = Vec::new();
    for (i, c) in header.iter().enumerate() {
        let key = c.trim().to_lowercase();
        match cols.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = i,
            None => cols.push((key, i)),
        }
    }
    let find = |needles: &[&str]| {
        cols.iter()
            .find(|(k, _)| needles.iter().all(|n| k.contains(n)))
            .map(|(_, i)| *i)
    };
    let date_col = find(&["date"]);
    let mut gross_col = if find(&["net", "total"]).is_some() {
        None
    } else {
        find(&["total", "return"])
    };
    if let Some((_, i)) = cols
        .iter()
        .find(|(k, _)| k.contains("total") && k.contains("return") && !k.contains("net"))
    {
        gross_col = Some(*i);
    }
    let net_col = find(&["net", "total"]);
    let Some(date_col) = date_col.filter(|_| gross_col.is_some() || net_col.is_some()) else {
        let names = PyValue::List(header.iter().map(|h| PyValue::Str(h.clone())).collect());
        return Err(BenchError::Value(format!(
            "{shown}: expected Date + 'Total Returns Index' columns, got {}",
            names.py_repr()
        )));
    };

    let mut out: Vec<TriRow> = Vec::new();
    for (n, row) in it.enumerate() {
        if row.is_empty() {
            continue;
        }
        let line_no = n + 2;
        if row.len() > header.len() && row[header.len()..].iter().any(|v| !v.trim().is_empty()) {
            return Err(BenchError::Value(format!(
                "{shown}: line {line_no} has more fields than the header — a value with a thousands separator is probably unquoted. Re-export or quote it; parsing on would silently mis-read the levels."
            )));
        }
        let Some(date) = parse_tri_date(row.get(date_col).map_or("", String::as_str)) else {
            continue;
        };
        let num = |col: Option<usize>| -> Option<f64> {
            let raw = row.get(col?)?.replace(',', "");
            crate::settings::py_float(raw.trim())
        };
        let (gross, net) = (num(gross_col), num(net_col));
        let Some(tri) = gross.or(net) else { continue };
        if out.iter().any(|r| r.date == date) {
            continue;
        }
        out.push(TriRow {
            date,
            tri,
            net_tri: net,
        });
    }
    if out.is_empty() {
        return Err(BenchError::Value(format!("{shown}: no parseable rows")));
    }
    out.sort_by(|a, b| a.date.cmp(&b.date));
    Ok(out)
}

// =============================================================================================
// persistence
// =============================================================================================

/// `upsert_benchmark(...)`'s dict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Upserted {
    /// `index_name`.
    pub index_name: String,
    /// `kind`.
    pub kind: String,
    /// `rows`.
    pub rows: usize,
    /// `first`.
    pub first: Option<String>,
    /// `last`.
    pub last: Option<String>,
}

/// `upsert_benchmark(conn, index_name, rows, kind=)`: fills only its own column (`close` for
/// PRI, `tri` for TRI), in one transaction. `pairs` are `(date, value)`.
///
/// # Errors
/// `ValueError` for an unknown kind; the database.
pub async fn upsert_benchmark(
    conn: &mut Conn,
    index_name: &str,
    pairs: &[(String, f64)],
    kind: &str,
) -> Result<Upserted, BenchError> {
    if kind != PRI && kind != TRI {
        return Err(BenchError::Value(format!(
            "kind must be {PRI} or {TRI}, got {}",
            PyValue::Str(kind.to_owned()).py_repr()
        )));
    }
    let (col, other) = if kind == PRI {
        ("close", "tri")
    } else {
        ("tri", "close")
    };
    let sql = format!(
        "INSERT INTO benchmark(index_name, date, {col}) VALUES(?,?,?) ON CONFLICT(index_name, date) DO UPDATE SET   {col}=excluded.{col}, {other}=COALESCE(benchmark.{other}, NULL)"
    );
    let seq: Vec<Vec<Param>> = pairs
        .iter()
        .map(|(d, v)| {
            vec![
                Param::from(index_name),
                Param::from(d.as_str()),
                Param::Float(*v),
            ]
        })
        .collect();
    conn.transaction(move |c| {
        Box::pin(async move {
            c.executemany(&sql, &seq).await?;
            Ok(())
        })
    })
    .await?;
    Ok(Upserted {
        index_name: index_name.to_owned(),
        kind: kind.to_owned(),
        rows: pairs.len(),
        first: pairs.first().map(|p| p.0.clone()),
        last: pairs.last().map(|p| p.0.clone()),
    })
}

/// A dated series as `upsert_benchmark` writes it (NaN dropped, ISO dates).
#[must_use]
pub fn series_pairs(s: &[(NaiveDate, f64)]) -> Vec<(String, f64)> {
    s.iter()
        .filter(|p| !p.1.is_nan())
        .map(|(d, v)| (d.format("%Y-%m-%d").to_string(), *v))
        .collect()
}

/// TRI rows as `upsert_benchmark` writes them.
#[must_use]
pub fn tri_pairs(rows: &[TriRow]) -> Vec<(String, f64)> {
    rows.iter()
        .filter(|r| !r.tri.is_nan())
        .map(|r| (r.date.clone(), r.tri))
        .collect()
}

fn db_f64(v: Option<&DbValue>) -> Option<f64> {
    match v? {
        DbValue::Null => None,
        DbValue::Text(s) => crate::settings::py_float(s),
        other => other.as_f64(),
    }
}

fn db_date(v: Option<&DbValue>) -> Option<NaiveDate> {
    match v? {
        DbValue::Text(s) => NaiveDate::parse_from_str(s.get(..10).unwrap_or(s), "%Y-%m-%d").ok(),
        DbValue::Naive(t) => Some(t.date()),
        DbValue::Aware(t) => Some(t.date_naive()),
        _ => None,
    }
}

/// `benchmark_series(conn, index_name, prefer=)`: TRI first (falling back to PRI) or the
/// reverse, labelled with what was used.
///
/// # Errors
/// The database.
pub async fn benchmark_series(
    conn: &mut Conn,
    index_name: &str,
    prefer: &str,
) -> Result<BenchSeries, StorageError> {
    let rows = conn
        .execute(
            "SELECT date, close, tri FROM benchmark WHERE index_name=? ORDER BY date",
            &[Param::from(index_name)],
        )
        .await?
        .rows;
    let order: [&'static str; 2] = if prefer == TRI {
        [TRI, PRI]
    } else {
        [PRI, TRI]
    };
    for kind in order {
        let col = if kind == TRI { "tri" } else { "close" };
        let mut s: Series = rows
            .iter()
            .filter_map(|r| Some((db_date(r.get("date"))?, db_f64(r.get(col))?)))
            .filter(|p| !p.1.is_nan())
            .collect();
        if !s.is_empty() {
            s.sort_by_key(|p| p.0);
            return Ok(BenchSeries {
                series: s,
                series_type: Some(kind),
                index_name: index_name.to_owned(),
            });
        }
    }
    Ok(BenchSeries {
        series: Vec::new(),
        series_type: None,
        index_name: index_name.to_owned(),
    })
}

/// `available_benchmarks(conn)`: coverage per index (`index_name, n, pri, tri, first, last`).
///
/// # Errors
/// The database.
pub async fn available_benchmarks(conn: &mut Conn) -> Result<Vec<Row>, StorageError> {
    Ok(conn
        .execute(
            "SELECT index_name, COUNT(*) n, SUM(close IS NOT NULL) pri, SUM(tri IS NOT NULL) tri,       MIN(date) first, MAX(date) last FROM benchmark GROUP BY index_name ORDER BY index_name",
            &[],
        )
        .await?
        .rows)
}
