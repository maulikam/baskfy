//! The generated scan — `app/scan_source.py` (the desk's half of the CSV cord cut) and the
//! pure `baskfy_core.momentum_scan` it calls, ported together because the desk is the only
//! caller (DECISIONS-RS R8.WP1).
//!
//! `(as_of, the screener's bars, the four carried columns) → the desk's columns`, under the
//! export's names, at the factor engine's storage precision, then crossed into the scorer's
//! [`Frame`] exactly as `pd.DataFrame(scan.frame.to_dicts())` types it:
//!
//! * a computed float column → `float64` (NULL → NaN), **or `object` of `None` when every cell
//!   is NULL** — pandas builds an all-`None` column as `object`, and comparing a float series to
//!   it raises `TypeError` (a 500 at `/analyze`); [`super::score`] reproduces that raise;
//! * a carried column keeps the dtype `pd.read_csv` gave it, through `pl.DataFrame(records)`
//!   and back — except a missing cell of a string column, which polars stores as the string
//!   `"NaN"` (pandas' `to_dict` hands it a float `nan`), and so does this port.
//!
//! The screener's database is read, never written (`test_the_scan_source_only_ever_reads`).
//! Nothing here reaches Kite or the gateway: a generated scan is an input to `/analyze`, the same
//! as an uploaded one, and the plan it builds still needs `/execute` with `confirm=true`.

use std::collections::{BTreeSet, HashMap};

use baskfy_core::CoreError;
use baskfy_core::factors::{FactorConfig, compute_factors};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, DataType, IntoColumn, NamedFrom, Series};
use sha2::{Digest, Sha256};

use super::frame::{Column, Frame};
use super::score::{ScoringConfig, dedupe};
use crate::wire::{PyDict, PyValue, dict};

/// `SPLIT_SIGNATURE`: an overnight close-to-close move this large is a split, not a price move.
pub const SPLIT_SIGNATURE: f64 = 0.35;
/// `MAX_ADJACENT_DAYS`: calendar days two bars may be apart and still count as consecutive.
pub const MAX_ADJACENT_DAYS: i64 = 7;
/// `CARRIED_COLUMNS`: the columns no bar series can produce.
pub const CARRIED_COLUMNS: [&str; 5] = [
    "marketcap",
    "beta",
    "circuits_three_months",
    "circuits_one_year",
    "is_nifty_fno",
];
/// `build`'s defaults.
pub const UNIVERSE: &str = "nse_cash";
/// `build`'s `data_version` default.
pub const DATA_VERSION: i64 = 1;
/// `build`'s `definition` default.
pub const DEFINITION: &str = "desk_momentum_v1";

/// `_WINDOW_SUFFIX` (`reference_export`), in its dict order.
const WINDOW_SUFFIX: [(i64, &str); 5] = [
    (12, "one_year"),
    (9, "nine_months"),
    (6, "six_months"),
    (3, "three_months"),
    (1, "one_month"),
];

/// `reference_export.FACTOR_COLUMN_MAP`: export column → factor column, in its dict order.
#[must_use]
pub fn factor_column_map() -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = [
        ("close", "close"),
        ("marketcap", "marketcap_cr"),
        ("volume", "vol_day_val"),
        ("median_volume_one_year", "median_vol_12m"),
        ("beta", "beta_12m"),
        ("high_one_year", "high_1y"),
        ("high_all_time", "high_ath"),
        ("away_from_high_one_year", "away_high_1y"),
        ("away_from_high_all_time", "away_high_ath"),
        ("ma_200", "ma_200"),
        ("ma_100", "ma_100"),
        ("ma_50", "ma_50"),
        ("ma_20", "ma_20"),
        ("series", "series"),
    ]
    .iter()
    .map(|(a, b)| ((*a).to_owned(), (*b).to_owned()))
    .collect();
    for (family, factor) in [
        ("absolute_return", "ret"),
        ("sharpe_return", "sharpe"),
        ("rsi", "rsi"),
        ("volatility", "vol"),
        ("circuits", "circuits"),
        ("positive_days_percent", "pos_days"),
    ] {
        for (m, s) in WINDOW_SUFFIX {
            out.push((format!("{family}_{s}"), format!("{factor}_{m}m")));
        }
    }
    out
}

/// `_EXPORT_NAME`: factor column → export column (`{v: k for k, v in FACTOR_COLUMN_MAP}`).
fn export_name() -> HashMap<String, String> {
    factor_column_map()
        .into_iter()
        .map(|(export, factor)| (factor, export))
        .collect()
}

/// `screen_run_id(definition, as_of, data_version)`: the first 16 hex digits of a SHA-256 over
/// the inputs, so the same inputs give the same id on any machine.
#[must_use]
pub fn screen_run_id(definition: &str, as_of: NaiveDate, data_version: i64) -> String {
    let digest = Sha256::digest(format!("{definition}|{as_of}|{data_version}").as_bytes());
    let mut s = String::with_capacity(16);
    for b in digest.iter().take(8) {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// One bar, as `_fetch` reads it (`float(x) if x is not None else 0.0` for every number).
#[derive(Debug, Clone, PartialEq)]
pub struct Bar {
    /// `i.symbol`.
    pub symbol: String,
    /// `b.instrument_id`.
    pub instrument_id: i64,
    /// `b.date`.
    pub date: NaiveDate,
    /// `b.open`.
    pub open: f64,
    /// `b.high`.
    pub high: f64,
    /// `b.low`.
    pub low: f64,
    /// `b.close`.
    pub close: f64,
    /// `b.volume`.
    pub volume: f64,
    /// `b.close_raw`.
    pub close_raw: f64,
    /// `b.volume_raw`.
    pub volume_raw: f64,
}

/// One scan, and the record of how it was made (`MomentumScan`). `frame` is already the
/// scorer's [`Frame`] in `required(cfg)` order, sorted by symbol (before `as_desk_frame`'s
/// de-duplication).
#[derive(Debug, Clone, PartialEq)]
pub struct MomentumScan {
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `universe`.
    pub universe: String,
    /// `frame` (as pandas types `to_dicts()`).
    pub frame: Frame,
    /// `screen_run_id`.
    pub screen_run_id: String,
    /// `data_version`.
    pub data_version: i64,
    /// `carried`.
    pub carried: Vec<String>,
    /// `suspect_symbols`.
    pub suspect_symbols: Vec<String>,
    /// Which cells of `frame` polars holds as NULL (pandas sees NaN or `None`; polars writes an
    /// empty field where a carried NaN is written `NaN`) — for [`MomentumScan::to_csv`].
    pub nulls: HashMap<String, Vec<bool>>,
    /// Columns polars holds as integers (an integer column with a NULL is `float64` to pandas
    /// but still printed as an integer by polars' CSV writer).
    pub int_columns: BTreeSet<String>,
}

impl MomentumScan {
    /// `to_csv()`: exactly what the desk's `load_scan` reads — UTF-8 with a BOM, the export's
    /// column names, as polars' `write_csv` prints them (NULL empty, NaN `NaN`, floats in their
    /// shortest round-trip form, booleans lower-case, a field quoted when it holds a comma, a
    /// quote or a line break).
    #[must_use]
    pub fn to_csv(&self) -> Vec<u8> {
        let quote = |s: &str| {
            if s.contains([',', '"', '\n', '\r']) {
                format!("\"{}\"", s.replace('"', "\"\""))
            } else {
                s.to_owned()
            }
        };
        let mut out = String::from("\u{feff}");
        out.push_str(
            &self
                .frame
                .names
                .iter()
                .map(|n| quote(n))
                .collect::<Vec<_>>()
                .join(","),
        );
        out.push('\n');
        for i in 0..self.frame.nrows {
            let cells: Vec<String> = self
                .frame
                .names
                .iter()
                .map(|n| {
                    if self.nulls.get(n).and_then(|m| m.get(i)).copied() == Some(true) {
                        return String::new();
                    }
                    match self.frame.col(n) {
                        Some(Column::Int(v)) => v[i].to_string(),
                        #[allow(clippy::cast_possible_truncation)]
                        Some(Column::Float(v)) if self.int_columns.contains(n) => {
                            (v[i] as i64).to_string()
                        }
                        Some(Column::Float(v)) => polars_float(v[i]),
                        Some(Column::Bool(v)) => if v[i] { "true" } else { "false" }.to_owned(),
                        Some(Column::Str(v)) => v[i].as_deref().map(quote).unwrap_or_default(),
                        None => String::new(),
                    }
                })
                .collect();
            out.push_str(&cells.join(","));
            out.push('\n');
        }
        out.into_bytes()
    }

    /// `provenance()`: what a plan records about the scan that built it, in Python's key order.
    #[must_use]
    pub fn provenance(&self) -> PyDict {
        let strs =
            |v: &[String]| PyValue::List(v.iter().map(|s| PyValue::Str(s.clone())).collect());
        dict([
            ("screen_run_id", PyValue::Str(self.screen_run_id.clone())),
            (
                "as_of",
                PyValue::Str(self.as_of.format("%Y-%m-%d").to_string()),
            ),
            ("universe", PyValue::Str(self.universe.clone())),
            ("data_version", PyValue::Int(self.data_version)),
            ("carried_columns", strs(&self.carried)),
            (
                "rows",
                PyValue::Int(i64::try_from(self.frame.nrows).unwrap_or(i64::MAX)),
            ),
            ("suspect_symbols", strs(&self.suspect_symbols)),
        ])
    }
}

/// Why a scan could not be generated: Python's exception text (`str(exc)`), which `/analyze`
/// prints after `could not generate a scan for {as_of}: `.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct GenerateError(pub String);

impl From<CoreError> for GenerateError {
    fn from(e: CoreError) -> Self {
        match e {
            // `ValueError(msg)` prints as `msg`.
            CoreError::InvalidInput(m) => Self(m),
            other => Self(other.to_string()),
        }
    }
}

impl From<polars::error::PolarsError> for GenerateError {
    fn from(e: polars::error::PolarsError) -> Self {
        Self(e.to_string())
    }
}

/// A Python list of strings' repr: `['a', 'b']`.
fn py_list(items: &[String]) -> String {
    let inner: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s))
        .collect();
    format!("[{}]", inner.join(", "))
}

fn days_since_epoch(d: NaiveDate) -> i32 {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap_or_default();
    i32::try_from(d.signed_duration_since(epoch).num_days()).unwrap_or(i32::MIN)
}

/// `_fetch`'s frame: `symbol, instrument_id, date, open, high, low, close, volume, close_raw,
/// volume_raw`, in the query's order.
///
/// # Errors
/// A column polars cannot build.
pub fn bars_frame(bars: &[Bar]) -> Result<DataFrame, GenerateError> {
    let f = |g: fn(&Bar) -> f64, name: &str| {
        Series::new(name.into(), bars.iter().map(g).collect::<Vec<f64>>()).into_column()
    };
    let date = Series::new(
        "date".into(),
        bars.iter()
            .map(|b| days_since_epoch(b.date))
            .collect::<Vec<i32>>(),
    )
    .cast(&DataType::Date)?
    .into_column();
    let columns = vec![
        Series::new(
            "symbol".into(),
            bars.iter()
                .map(|b| b.symbol.clone())
                .collect::<Vec<String>>(),
        )
        .into_column(),
        Series::new(
            "instrument_id".into(),
            bars.iter().map(|b| b.instrument_id).collect::<Vec<i64>>(),
        )
        .into_column(),
        date,
        f(|b| b.open, "open"),
        f(|b| b.high, "high"),
        f(|b| b.low, "low"),
        f(|b| b.close, "close"),
        f(|b| b.volume, "volume"),
        f(|b| b.close_raw, "close_raw"),
        f(|b| b.volume_raw, "volume_raw"),
    ];
    Ok(DataFrame::new(bars.len(), columns)?)
}

/// `unadjusted_symbols(bars, threshold)`: symbols whose history holds a close-to-close step no
/// market makes between two bars at most [`MAX_ADJACENT_DAYS`] apart, sorted.
#[must_use]
pub fn unadjusted_symbols(bars: &[Bar], threshold: f64) -> Vec<String> {
    let mut order: Vec<usize> = (0..bars.len()).collect();
    // `sort(["symbol", "date"])` (polars' default sort is not stable; ties are equal rows).
    order.sort_by(|&a, &b| {
        bars[a]
            .symbol
            .cmp(&bars[b].symbol)
            .then(bars[a].date.cmp(&bars[b].date))
    });
    let mut out: BTreeSet<String> = BTreeSet::new();
    for w in order.windows(2) {
        let (p, c) = (&bars[w[0]], &bars[w[1]]);
        if p.symbol != c.symbol {
            continue;
        }
        // `_prev > 0` (a NaN close is never > 0, and a NaN step is never > threshold).
        if p.close.is_nan() || p.close <= 0.0 {
            continue;
        }
        if c.date.signed_duration_since(p.date).num_days() > MAX_ADJACENT_DAYS {
            continue;
        }
        if ((c.close / p.close) - 1.0).abs() > threshold {
            out.insert(c.symbol.clone());
        }
    }
    out.into_iter().collect()
}

/// A computed column as `pd.DataFrame(frame.to_dicts())` types it.
fn pandas_column(s: &polars::prelude::Column) -> Result<Column, GenerateError> {
    let s = s.as_materialized_series();
    let n = s.len();
    let nulls = s.null_count();
    let dtype = s.dtype().clone();
    if dtype == DataType::String {
        return Ok(Column::Str(
            s.str()?.iter().map(|v| v.map(str::to_owned)).collect(),
        ));
    }
    if n > 0 && nulls == n {
        // Every cell `None`: pandas builds an `object` column of `None`.
        return Ok(Column::Str(vec![None; n]));
    }
    if dtype == DataType::Boolean {
        if nulls == 0 {
            return Ok(Column::Bool(
                s.bool()?.iter().map(|v| v.unwrap_or(false)).collect(),
            ));
        }
        // A bool column with a hole is `object` (True/False/None) in pandas; the scan carries
        // none, and its cells print as Python would.
        return Ok(Column::Str(
            s.bool()?
                .iter()
                .map(|v| v.map(|b| if b { "True" } else { "False" }.to_owned()))
                .collect(),
        ));
    }
    if dtype.is_integer() && nulls == 0 {
        let c = s.cast(&DataType::Int64)?;
        return Ok(Column::Int(
            c.i64()?.iter().map(|v| v.unwrap_or(0)).collect(),
        ));
    }
    if dtype.is_primitive_numeric() {
        let c = s.cast(&DataType::Float64)?;
        return Ok(Column::Float(
            c.f64()?.iter().map(|v| v.unwrap_or(f64::NAN)).collect(),
        ));
    }
    // A date or anything else: its string form (no required column is one).
    let c = s.cast(&DataType::String)?;
    Ok(Column::Str(
        c.str()?.iter().map(|v| v.map(str::to_owned)).collect(),
    ))
}

/// A carried column after `pl.DataFrame(pandas.to_dict(orient="records"))` and back: the
/// dtype is kept; a missing string becomes the string `"NaN"`.
fn carried_roundtrip(c: &Column) -> Column {
    match c {
        Column::Str(v) => Column::Str(
            v.iter()
                .map(|x| Some(x.clone().unwrap_or_else(|| "NaN".to_owned())))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// `momentum_scan.build(bars, as_of, trading_days, cfg=cfg, carried=carried)` with the
/// defaults (`universe="nse_cash"`, `data_version=1`, `definition="desk_momentum_v1"`, no
/// benchmark, the default factor configuration).
///
/// `carried` is the newest retained scan as `pd.read_csv` read it; its `symbol`, `series` and
/// [`CARRIED_COLUMNS`] are taken (`_carried_columns`).
///
/// # Errors
/// Python's `ValueError` text: a `carried` column missing, the factor engine's refusals (an
/// `as_of` that is not a trading day, …), or a required column the engine did not produce.
pub fn build(
    bars: &[Bar],
    as_of: NaiveDate,
    trading_days: &[NaiveDate],
    cfg: &ScoringConfig,
    carried: &Frame,
) -> Result<MomentumScan, GenerateError> {
    let carried_names: Vec<&str> = std::iter::once("symbol")
        .chain(std::iter::once("series"))
        .chain(CARRIED_COLUMNS.iter().copied())
        .collect();
    let missing: Vec<String> = std::iter::once("symbol")
        .chain(CARRIED_COLUMNS.iter().copied())
        .filter(|c| !carried.has(c))
        .map(ToOwned::to_owned)
        .collect();
    if !missing.is_empty() {
        return Err(GenerateError(format!(
            "`carried` is missing {}; see CARRIED_COLUMNS",
            py_list(&missing)
        )));
    }
    let frame = bars_frame(bars)?;
    let computed = compute_factors(
        &frame,
        as_of,
        trading_days,
        None,
        &FactorConfig::default(),
        None,
    )?
    .frame;

    // Rename to the export's names; a column the rename would shadow is dropped first.
    let to_export = export_name();
    let names: Vec<String> = computed
        .get_column_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let renames: Vec<(String, String)> = names
        .iter()
        .filter_map(|c| to_export.get(c).map(|d| (c.clone(), d.clone())))
        .collect();
    let shadowed: BTreeSet<String> = renames
        .iter()
        .filter(|(src, dst)| dst != src && names.contains(dst))
        .map(|(_, dst)| dst.clone())
        .collect();
    let mut cols: Vec<(String, Column)> = Vec::new();
    let mut null_masks: HashMap<String, Vec<bool>> = HashMap::new();
    let mut int_cols: BTreeSet<String> = BTreeSet::new();
    for c in &names {
        if shadowed.contains(c) {
            continue;
        }
        let out = to_export.get(c).cloned().unwrap_or_else(|| c.clone());
        let col = computed.column(c)?;
        let mask: Vec<bool> = col
            .as_materialized_series()
            .is_null()
            .iter()
            .map(|v| v.unwrap_or(false))
            .collect();
        null_masks.insert(out.clone(), mask);
        if col.dtype().is_integer() {
            int_cols.insert(out.clone());
        }
        cols.push((out, pandas_column(col)?));
    }
    // Whatever the caller carries wins wherever both sides have it.
    let carried_cols: Vec<(&str, Column)> = carried_names
        .iter()
        .filter_map(|c| carried.col(c).map(|col| (*c, carried_roundtrip(col))))
        .collect();
    cols.retain(|(name, _)| {
        name == "symbol"
            || !carried_cols
                .iter()
                .any(|(c, _)| *c != "symbol" && c == name)
    });

    // The inner join on symbol, then `sort("symbol")`.
    let sym_idx = cols.iter().position(|(n, _)| n == "symbol");
    let computed_symbols: Vec<Option<String>> = sym_idx
        .and_then(|i| cols.get(i))
        .map(|(_, c)| (0..c.len()).map(|r| c.str_at(r)).collect())
        .unwrap_or_default();
    let carried_symbols: Vec<Option<String>> = carried
        .col("symbol")
        .map(|c| match carried_roundtrip(c) {
            Column::Str(v) => v,
            other => (0..other.len()).map(|r| other.str_at(r)).collect(),
        })
        .unwrap_or_default();
    let mut by_symbol: HashMap<&str, Vec<usize>> = HashMap::new();
    for (j, s) in carried_symbols.iter().enumerate() {
        if let Some(s) = s {
            by_symbol.entry(s.as_str()).or_default().push(j);
        }
    }
    let mut pairs: Vec<(String, usize, usize)> = Vec::new();
    for (i, s) in computed_symbols.iter().enumerate() {
        if let Some(s) = s
            && let Some(js) = by_symbol.get(s.as_str())
        {
            for &j in js {
                pairs.push((s.clone(), i, j));
            }
        }
    }
    pairs.sort_by(|a, b| a.0.cmp(&b.0));
    let left: Vec<usize> = pairs.iter().map(|p| p.1).collect();
    let right: Vec<usize> = pairs.iter().map(|p| p.2).collect();

    let mut joined: HashMap<String, Column> = HashMap::new();
    for (name, col) in &cols {
        joined.insert(name.clone(), take(col, &left));
    }
    for (name, col) in &carried_cols {
        if *name != "symbol" {
            joined.insert((*name).to_owned(), take(col, &right));
        }
    }
    let iso = as_of.format("%Y-%m-%d").to_string();
    joined.insert("date".to_owned(), Column::Str(vec![Some(iso); pairs.len()]));

    let required = cfg.required();
    let absent: Vec<String> = required
        .iter()
        .filter(|c| !joined.contains_key(*c))
        .cloned()
        .collect();
    if !absent.is_empty() {
        return Err(GenerateError(format!(
            "the engine produced no {} \u{2014} the desk's `load_scan` would reject this scan. Either the factor engine lost a column or `FACTOR_COLUMN_MAP` no longer names it.",
            py_list(&absent)
        )));
    }
    let mut nulls: HashMap<String, Vec<bool>> = HashMap::new();
    let mut int_columns: BTreeSet<String> = BTreeSet::new();
    for c in &required {
        let carried_here = carried_cols.iter().any(|(n, _)| *n != "symbol" && n == c);
        if !carried_here && int_cols.contains(c) {
            int_columns.insert(c.clone());
        }
    }
    for c in &required {
        let carried_here = carried_cols.iter().any(|(n, _)| *n != "symbol" && n == c);
        if let (false, Some(m)) = (carried_here, null_masks.get(c)) {
            nulls.insert(
                c.clone(),
                left.iter()
                    .map(|&i| m.get(i).copied().unwrap_or(false))
                    .collect(),
            );
        }
    }
    let mut out = Frame {
        nrows: pairs.len(),
        ..Frame::default()
    };
    for c in &required {
        if let Some(col) = joined.remove(c) {
            out.names.push(c.clone());
            out.columns.insert(c.clone(), col);
        }
    }
    let in_scan: BTreeSet<&str> = pairs.iter().map(|p| p.0.as_str()).collect();
    let suspect = unadjusted_symbols(bars, SPLIT_SIGNATURE)
        .into_iter()
        .filter(|s| in_scan.contains(s.as_str()))
        .collect();
    Ok(MomentumScan {
        as_of,
        universe: UNIVERSE.to_owned(),
        frame: out,
        screen_run_id: screen_run_id(DEFINITION, as_of, DATA_VERSION),
        data_version: DATA_VERSION,
        carried: CARRIED_COLUMNS.iter().map(|s| (*s).to_owned()).collect(),
        suspect_symbols: suspect,
        nulls,
        int_columns,
    })
}

/// A float as polars' CSV writer prints it.
fn polars_float(x: f64) -> String {
    if x.is_nan() {
        return "NaN".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    baskfy_core::py::float_repr(x)
}

fn take(c: &Column, rows: &[usize]) -> Column {
    let one = Frame {
        names: vec!["c".to_owned()],
        columns: HashMap::from([("c".to_owned(), c.clone())]),
        nrows: c.len(),
    };
    one.take(rows)
        .columns
        .remove("c")
        .unwrap_or(Column::Float(Vec::new()))
}

/// `scan_source.generate(as_of, carried)` over bars already fetched: the empty-history refusal,
/// then [`build`].
///
/// # Errors
/// Python's `ValueError` text.
pub fn generate_from(
    bars: &[Bar],
    trading_days: &[NaiveDate],
    as_of: NaiveDate,
    cfg: &ScoringConfig,
    carried: &Frame,
) -> Result<MomentumScan, GenerateError> {
    if bars.is_empty() {
        return Err(GenerateError(format!(
            "no bars on or before {as_of} in the screener's database. Run `make backfill` there, or upload a scan CSV instead."
        )));
    }
    build(bars, as_of, trading_days, cfg, carried)
}

/// `as_desk_frame(scan)`: the scan as the scorer reads it, duplicate symbols dropped (first
/// kept). `Err` is the `ValueError: Generated scan missing columns: [...]` an empty scan raises
/// (`pd.DataFrame([])` has no columns at all), which `/analyze` does not catch (a 500).
///
/// # Errors
/// As above.
pub fn as_desk_frame(scan: &MomentumScan, cfg: &ScoringConfig) -> Result<Frame, GenerateError> {
    if scan.frame.nrows == 0 {
        return Err(GenerateError(format!(
            "Generated scan missing columns: {}",
            py_list(&cfg.required())
        )));
    }
    Ok(dedupe(&scan.frame))
}

/// `_scan_warnings(generated)`: what is wrong with this scan, said on the plan.
#[must_use]
pub fn scan_warnings(scan: &MomentumScan) -> Vec<String> {
    if scan.suspect_symbols.is_empty() {
        return Vec::new();
    }
    vec![format!(
        "{} of {} symbols carry an unadjusted corporate action: their highs and returns are wrong, and some will be rejected by the far_from_high filter. This scan is a SMALLER universe than an uploaded one. See NEEDS-MAULIK.md item 4.",
        scan.suspect_symbols.len(),
        scan.frame.nrows
    )]
}

// =============================================================================================
// The screener's database (read only)
// =============================================================================================

/// `_BARS_SQL` with `symbols=None` (the desk never narrows it), numbers read as text and parsed
/// (`float(Decimal)` is the correctly rounded double of the decimal string, and so is
/// `str::parse::<f64>`).
const BARS_SQL: &str = "select i.symbol, b.instrument_id::int8, b.date, b.open::text, b.high::text, b.low::text, \
       b.close::text, b.volume::text, b.close_raw::text, b.volume_raw::text \
  from ohlcv_daily b \
  join instrument i on i.id = b.instrument_id \
 where b.date <= $1::date \
 order by i.symbol, b.date";

/// `_DAYS_SQL`.
const DAYS_SQL: &str = "select distinct date from ohlcv_daily where date <= $1::date order by date";

/// `str(exc)` for a psycopg exception, as far as it can be reproduced from sqlx: libpq's
/// connection-refused text exactly (the case the harness exercises); otherwise the server's
/// message (DECISIONS-RS R8.WP1).
#[must_use]
pub fn psycopg_str(dsn: &str, e: &sqlx::Error) -> String {
    match e {
        sqlx::Error::Io(io) if io.kind() == std::io::ErrorKind::ConnectionRefused => {
            let (host, port) = host_port(dsn);
            format!(
                "connection failed: connection to server at \"{host}\", port {port} failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?"
            )
        }
        sqlx::Error::Database(d) => d.message().to_owned(),
        other => format!("connection failed: {other}"),
    }
}

fn host_port(dsn: &str) -> (String, String) {
    let rest = dsn.split_once("://").map_or(dsn, |(_, r)| r);
    let rest = rest.rsplit_once('@').map_or(rest, |(_, r)| r);
    let hostport = rest.split(['/', '?']).next().unwrap_or("");
    match hostport.rsplit_once(':') {
        Some((h, p)) => (h.to_owned(), p.to_owned()),
        None => (hostport.to_owned(), "5432".to_owned()),
    }
}

fn parse_num(v: Option<String>) -> f64 {
    v.and_then(|t| t.trim().parse::<f64>().ok()).unwrap_or(0.0)
}

/// `_fetch(as_of, None)`: every bar on or before `as_of` and the trading calendar.
///
/// # Errors
/// `str(exc)` of what psycopg would have raised.
pub async fn fetch(dsn: &str, as_of: NaiveDate) -> Result<(Vec<Bar>, Vec<NaiveDate>), String> {
    use sqlx::Connection as _;
    use sqlx::Row as _;
    let mut conn = sqlx::postgres::PgConnection::connect(dsn)
        .await
        .map_err(|e| psycopg_str(dsn, &e))?;
    let rows = sqlx::query(BARS_SQL)
        .bind(as_of)
        .fetch_all(&mut conn)
        .await
        .map_err(|e| psycopg_str(dsn, &e))?;
    let days_rows = sqlx::query(DAYS_SQL)
        .bind(as_of)
        .fetch_all(&mut conn)
        .await
        .map_err(|e| psycopg_str(dsn, &e))?;
    let _ = conn.close().await;
    let mut bars = Vec::with_capacity(rows.len());
    for r in &rows {
        let text = |i: usize| r.try_get::<Option<String>, _>(i).ok().flatten();
        let Ok(date) = r.try_get::<NaiveDate, _>(2) else {
            return Err("a bar without a date".to_owned());
        };
        bars.push(Bar {
            symbol: r.try_get::<String, _>(0).unwrap_or_default(),
            instrument_id: r.try_get::<i64, _>(1).unwrap_or_default(),
            date,
            open: parse_num(text(3)),
            high: parse_num(text(4)),
            low: parse_num(text(5)),
            close: parse_num(text(6)),
            volume: parse_num(text(7)),
            close_raw: parse_num(text(8)),
            volume_raw: parse_num(text(9)),
        });
    }
    let days = days_rows
        .iter()
        .filter_map(|r| r.try_get::<NaiveDate, _>(0).ok())
        .collect();
    Ok((bars, days))
}

/// `_latest_pipeline_date()`'s query: `select max(date) from ohlcv_daily`.
///
/// # Errors
/// `str(exc)` of what psycopg would have raised.
pub async fn latest_pipeline_date(dsn: &str) -> Result<Option<NaiveDate>, String> {
    use sqlx::Connection as _;
    use sqlx::Row as _;
    let mut conn = sqlx::postgres::PgConnection::connect(dsn)
        .await
        .map_err(|e| psycopg_str(dsn, &e))?;
    let row = sqlx::query("select max(date) from ohlcv_daily")
        .fetch_optional(&mut conn)
        .await
        .map_err(|e| psycopg_str(dsn, &e))?;
    let _ = conn.close().await;
    Ok(row.and_then(|r| r.try_get::<Option<NaiveDate>, _>(0).ok().flatten()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn screen_run_ids_are_a_digest_of_the_inputs() {
        let d = NaiveDate::from_ymd_opt(2018, 3, 1).unwrap_or_default();
        // hashlib.sha256(b"desk_momentum_v1|2018-03-01|1").hexdigest()[:16]
        assert_eq!(screen_run_id(DEFINITION, d, 1).len(), 16);
        assert_eq!(
            screen_run_id(DEFINITION, d, 1),
            screen_run_id(DEFINITION, d, 1)
        );
        assert_ne!(
            screen_run_id(DEFINITION, d, 1),
            screen_run_id(DEFINITION, d, 2)
        );
    }

    #[test]
    fn libpq_refusal_text() {
        let e = sqlx::Error::Io(std::io::Error::from(std::io::ErrorKind::ConnectionRefused));
        assert_eq!(
            psycopg_str("postgresql://nobody@127.0.0.1:1/none", &e),
            "connection failed: connection to server at \"127.0.0.1\", port 1 failed: Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?"
        );
    }
}
